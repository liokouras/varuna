from typing import TYPE_CHECKING, Any, Iterable, List, Optional, Tuple, Union, cast
import torch
from torch import Tensor, nn
import torch.distributed as dist
from torch.multiprocessing import Process
from queue import Queue
from threading import Thread
import math
from apex import amp

from .partitioned_model import PartitionedModel
import gc
# from hashlib import sha1

import os
import sys

Module = nn.Module

class Varuna(Module):
    """
    model = nn.Sequential(a,b,c,d)
    model = Varuna(model, microbatches/minibatch, list_of_devices)
    for iteration in epoch:
        model(input)   # execute Varuna's pipeline (forward and backward pass)
        optimizer.step()
        optimizer.zero_grad()
    """
    def __init__(self,
                model,
                stage_to_rank_map,
                dummy_inputs,
                batch_size,
                optimizer,
                chunk_size,
                fp16 = False, 
                local_rank=-1,
                device=-1):
        super().__init__()

        self.partitions = len(stage_to_rank_map)
        self.rank = dist.get_rank()
        self.local_rank = local_rank if local_rank != -1 else self.rank
        self.stage_to_rank_map = stage_to_rank_map

        self.stage = -1
        for stage in self.stage_to_rank_map:
            i = 0
            for rank in self.stage_to_rank_map[stage]:
                if rank == self.rank:
                    rank_within_stage = i
                    data_depth = len(self.stage_to_rank_map[stage])
                    self.stage = stage
                    break
                i += 1
        if self.stage == -1:
            raise ValueError("Rank " + str(self.rank) + " not found in stage to rank map!")

        if device == -1:
            device = self.local_rank
        torch.cuda.set_device(device)
        self.device = torch.device("cuda", device)

        self.optimizer = optimizer
        self.fp16 = fp16

        # partition model based on "CutPoint"s using a dry run with dummy inputs (dict)
        self.model = PartitionedModel(model, self.rank, self.local_rank, device, self.stage_to_rank_map, self.fp16)
        self.model.initialize( dummy_inputs, from_cache=False )
        self.partitioned_model = self.model

        # assert(batch_size % data_depth == 0, "Batch size not divisible by data parallel depth!")
        self.batch_size = batch_size // data_depth
        self.micro_batch_size = chunk_size
        self.last_chunk_size = self.batch_size % chunk_size 
        self.init_communication(rank_within_stage)

        self.model.to(self.device)
        self.init_distributed()


        self.config = {
            "stage": self.stage,
            "partitions": self.partitions,
            "fp16": self.fp16,
            "fwd_inp_shape": self.fwd_inp_shape,
            "bwd_grad_shape": self.bwd_grad_shape,
            "recieve_rank": self.recieve_rank,
            "send_rank": self.send_rank,
            "device": self.device,
            "data_parallel": bool(len(self.stage_to_rank_map[self.stage]) > 1),
            "last_chunk_size": self.last_chunk_size,
            "embedding_recv_rank": self.embedding_recv_rank,
            "embedding_send_rank": self.embedding_send_rank
        }

        self.schedule = self.generate_schedule()

    def init_communication(self, rank_within_stage):
        
        self.embedding_recv_rank = None
        self.embedding_send_rank = None
        if self.stage == 0:
            self.embedding_recv_rank = self.stage_to_rank_map[self.partitions-1][rank_within_stage]
        if self.stage == self.partitions - 1:
            self.embedding_send_rank = self.stage_to_rank_map[0][rank_within_stage]

        self.send_rank = None; self.recieve_rank = None

        # send ranks
        if self.stage < (self.partitions-1):
            self.send_rank = self.stage_to_rank_map[self.stage + 1][rank_within_stage]

        # recieve ranks
        if self.stage > 0:
            self.recieve_rank = self.stage_to_rank_map[self.stage - 1][rank_within_stage]

        # set expected shapes of inputs and gradients for each partition
        self.fwd_inp_shape = self.bwd_grad_shape = None
        if self.stage > 0:
            self.fwd_inp_shape = self.model.forward_input_shapes[0]
            self.fwd_inp_shape[0] = self.micro_batch_size
            # print("Varuna fwd inp shape ", self.fwd_inp_shape)
        if self.stage < (self.partitions-1):
            self.bwd_grad_shape = self.model.backward_grad_shapes[0]
            self.bwd_grad_shape[0] = self.micro_batch_size
            # print("Varuna bwd grad shape", self.bwd_grad_shape)

    def init_distributed(self):
        # create same process groups on all ranks
        process_groups = {}
        for stage in range(self.partitions):
            ranks = self.stage_to_rank_map[stage]
            if len(ranks) > 1:
                process_groups[stage] = dist.new_group(ranks=ranks)
            else:
                process_groups[stage] = None

        if process_groups[self.stage] is not None:
            self.partitioned_model = self.model
            self.model = torch.nn.parallel.DistributedDataParallel(self.model, process_group=process_groups[self.stage], device_ids=[self.device], find_unused_parameters=True)    
    
    def forward(self, inputs):        
        # Divide a mini-batch into micro-batches.
        batches = scatter(inputs, self.micro_batch_size)
        
        # need not pass the first argument if rank!=0
        # avoid dataloader compute in machines other than the first
        # ask the model writer to pass the input batch generating dataloader function to Varuna::__init__
        # and Varuna can take care of input dataloader explicitly
        pipeline = Pipeline(batches, self.model, self.config, self.schedule, self.optimizer)
        loss = pipeline.run()
        return loss
    
    def eval(self):
        self.model.eval()
    
    def train(self):
        self.model.train()

    def zero_grad(self):
        self.model.zero_grad()
    
    def checkpoint(self, cp_dir_name):
        return self.partitioned_model.checkpoint(cp_dir_name)

    def to(self, device):
        self.model.to(device)
    
    def generate_schedule(self):
        chunks = math.ceil(self.batch_size / self.micro_batch_size)
        print(chunks,"chunks")
        c_schedule = os.popen(os.path.join(os.path.dirname(os.path.abspath(__file__)),'genschedule ')+str(self.partitions)+' '+str(chunks)+' '+str(self.stage)).read()
        schedule = list()

        steps = c_schedule.split(';')
        steps = steps[:-1]
        for step in steps:
            task = step.split(',')
            schedule.append((int(task[0]), int(task[1])))
        
        return schedule
                

def save_rng_states(device):
    """capture current CPU, GPU random number generator states to reuse while recomputing activations
    in order to ensure Referential Transparency
    """
    cpu_rng_state = torch.get_rng_state()

    gpu_rng_states: Optional[ByteTensor]
    # gpu_rng_states = torch.cuda.get_rng_state_all() 
    gpu_rng_states = torch.cuda.get_rng_state(device)
    return (cpu_rng_state, gpu_rng_states)

def restore_rng_states(rng_states, device):
    cpu_rng_state, gpu_rng_states = rng_states
    torch.set_rng_state(cpu_rng_state)
    # torch.cuda.set_rng_state_all(gpu_rng_states)        # todo: verify correctness;   batchNorm, dropouts, convlayers?
    torch.cuda.set_rng_state(gpu_rng_states, device)


class Pipeline:
    """ Pipeline parallelism for Varuna """

    def __init__(self, batches, model, config, schedule, optimizer):
        self.batches = batches
        self.partitions = config["partitions"]
        self.stage = config["stage"]
        self.data_parallel = config["data_parallel"]

        self.model = model
        self.partitioned_model = self.model.module if self.data_parallel else self.model
        self.device = config["device"]
        self.world_size = self.partitions
        self.schedule = schedule
        # print(self.schedule)

        # print(self.model)
        # if (self.world_size>1):
        #     if (self.stage == 0):
        #         # print('fp16 module = ', self.model)
        #         dist.send(self.model.module.bert.embeddings.word_embeddings.weight.cpu(), self.world_size-1)
        #     elif (self.stage == self.world_size-1):
        #         encoder_weights = torch.FloatTensor(self.model.module.cls.predictions.decoder.weight.size()).half()
        #         dist.recv(encoder_weights, 0)
        #         # mask = torch.ones(encoder_weights.size()).byte().to(self.device)
        #         # self.model.module.model.cls.predictions.decoder.weight = self.model.module.model.cls.predictions.decoder.weight.masked_scatter(mask, encoder_weights.to(self.device))
        #         self.model.module.cls.predictions.decoder.weight = torch.nn.Parameter(encoder_weights.to(self.device))

        baseModule = self.model.module if not self.data_parallel else self.model.module.module

        if self.partitions > 1:
            if self.stage == self.partitions - 1:
                dist.send(baseModule.cls.predictions.decoder.weight.cpu(), config["embedding_send_rank"])
            elif self.stage == 0:
                decoder_weights = torch.FloatTensor(baseModule.bert.embeddings.word_embeddings.weight.size())#.half()
                dist.recv(decoder_weights, config["embedding_recv_rank"])
                baseModule.bert.embeddings.word_embeddings.weight = torch.nn.Parameter(decoder_weights.to(self.device))


        self.fwd_inp_shape = config["fwd_inp_shape"]
        self.bwd_grad_shape = config["bwd_grad_shape"]
        
        self.recieve_rank = config["recieve_rank"]
        self.send_rank = config["send_rank"]

        self.last_chunk_size = config["last_chunk_size"]

        self.optimizer = optimizer
        self.fp16 = config["fp16"]

        self.grads_send_queue = Queue()
        self.acts_send_queue = Queue()
        self.spawn_send_workers()

        self.acts_queue = Queue()       # activation at the boundary, rename as input_acts
        self.grads_queue = Queue()
        self.recompute_queue = Queue()

        self.acts_recieve_thread = None
        self.grads_recieve_thread = None
        self.acts_send_thread = None
        self.grads_send_thread = None

        # stores output of recompute(/forward) pass to be used by backward()
        self.loss = None
        self.average_loss = 0

    def spawn_recieve_workers(self):
        if self.stage > 0:
            self.acts_recieve_thread = Thread(target=self.acts_reciever, args=())
            self.acts_recieve_thread.daemon=True
            self.acts_recieve_thread.start()

        if self.stage < self.world_size-1:
            self.grads_recieve_thread = Thread(target=self.grads_reciever, args=())
            self.grads_recieve_thread.daemon=True
            self.grads_recieve_thread.start()
    
    def spawn_send_workers(self):
        if self.stage < self.world_size-1:
            self.acts_send_thread = Thread(target=self.acts_sender, args=())
            self.acts_send_thread.daemon=True
            self.acts_send_thread.start()

        if self.stage > 0:
            self.grads_send_thread = Thread(target=self.grads_sender, args=())
            self.grads_send_thread.daemon=True
            self.grads_send_thread.start() 
    
    def acts_reciever(self):
        chunks = len(self.batches)
        dtype = torch.float16 if self.fp16 else torch.float32
        for task,index in self.schedule:
            if task == 0:
                fwd_inp_shape = self.fwd_inp_shape
                if index == (chunks-1) and self.last_chunk_size > 0:
                    fwd_inp_shape = list(self.fwd_inp_shape)
                    fwd_inp_shape[0] = self.last_chunk_size
                acts_tensor = torch.ones(fwd_inp_shape, dtype=dtype)
                # print("stage", self.stage, "expecting acts of shape", fwd_inp_shape)
                handle = dist.irecv(acts_tensor, src=self.recieve_rank)
                handle.wait()
                self.acts_queue.put(acts_tensor.to(self.device))
        del acts_tensor
    
    def grads_reciever(self):
        chunks = len(self.batches)
        dtype = torch.float16 if self.fp16 else torch.float32
        for task,index in self.schedule:
            if task == 2:
                bwd_grad_shape = self.bwd_grad_shape
                if index == (chunks-1) and self.last_chunk_size > 0:
                    bwd_grad_shape = list(self.bwd_grad_shape)
                    bwd_grad_shape[0] = self.last_chunk_size
                grads_tensor = torch.ones(bwd_grad_shape, dtype=dtype)
                # print("stage", self.stage, "expecting grads of shape", bwd_grad_shape)
                handle = dist.irecv(grads_tensor, src=self.send_rank)
                handle.wait()
                self.grads_queue.put(grads_tensor.to(self.device))
        del grads_tensor

    def acts_sender(self):
        count = 0
        for task,index in self.schedule:
            if task == 0:
                count += 1
        while count > 0:
            output_acts = self.acts_send_queue.get()
            # print("stage", self.stage, "sending acts of shape", output_acts.size())
            handle = dist.isend(output_acts.cpu(), dst=self.send_rank)
            handle.wait()
            del output_acts, handle
            count -= 1

    def grads_sender(self):
        count = 0
        for task,index in self.schedule:
            if task == 2:
                count += 1
        while count > 0:
            input_grads = self.grads_send_queue.get()
            # print("stage", self.stage, "sending grads of shape", input_grads.size())
            handle = dist.isend(input_grads.cpu(), dst=self.recieve_rank)
            handle.wait()
            del input_grads, handle
            count -= 1
        
    # tells the model where to send acts and gradients
    def set_model_send_fn(self, recompute = False):
        def send(tensor, grads = False):
            if grads:
                self.grads_send_queue.put(tensor)
            else:
                if not recompute:
                    self.acts_send_queue.put(tensor)
        
        self.partitioned_model.set_send_fn(send)

    # tells the model how to recieve acts and gradients
    def set_model_recv_fn(self, recompute = False):
        if recompute:
            ctx, acts = self.recompute_queue.get()
            restore_rng_states(ctx, self.device)

            # cpu_rng_state = torch.get_rng_state()
            # gpu_rng_states: Optional[ByteTensor]
            # gpu_rng_states = torch.cuda.get_rng_state(self.device)
            # print('recompute: srngs: cpu = ', sha1(cpu_rng_state.cpu().numpy()).hexdigest(), '  gpu = ', sha1(gpu_rng_states.cpu().numpy()).hexdigest())
        else:
            acts = self.acts_queue.get() if self.stage > 0 else None

        def recv(grads = False):
            if grads:
                return self.grads_queue.get()
            else:
                return acts
        
        self.partitioned_model.set_recv_fn(recv)
        # because there's no peek/front method for these queues
        return acts

    def worker(self, task, grad_mode, inputs_as_dict):
        """ Main body of worker loop """
        world_size = self.world_size

        if task == 0:       
            torch.set_grad_enabled(grad_mode)

            rng_states=None
            if grad_mode == False:
                # if these acts are going to be recomputed
                rng_states = save_rng_states(self.device)

            self.set_model_send_fn(recompute = False)
            acts = self.set_model_recv_fn(recompute = False)
            output = self.model(**inputs_as_dict)

            if grad_mode == False:
                ctx = (rng_states, acts)
                self.recompute_queue.put(ctx)
            else:
                # save loss and input activations for the backward pass to use
                self.loss = output[0] if isinstance(output,tuple) else output

            
        elif task == 1:
            torch.set_grad_enabled(True)
            self.set_model_send_fn(recompute = True)
            self.set_model_recv_fn(recompute = True)
            output = self.model(**inputs_as_dict)

            self.loss = output[0] if isinstance(output,tuple) else output
        
        else:
            if self.stage != world_size-1:
                grads = torch.ones(self.loss.size(), dtype = torch.float32).to(self.device)
                if self.fp16:
                    with amp.scale_loss(self.loss, self.optimizer, delay_overflow_check=True) as scaled_loss:
                        scaled_loss.backward(grads)
                    # self.optimizer.backward(self.loss, grads=grads)
                    # self.loss.backward(grads)
                else:
                    self.loss.backward(grads)

            else:
                chunks = len(self.batches)
                self.loss = self.loss/chunks
                self.average_loss += self.loss.item()

                if self.fp16:
                    with amp.scale_loss(self.loss, self.optimizer, delay_overflow_check=False) as scaled_loss:
                        scaled_loss.backward()
                    # self.optimizer.backward(self.loss)
                else:
                    self.loss.backward()

            del self.loss
            self.loss = None
        
    def run(self):
        self.spawn_recieve_workers()

        for index, task in enumerate(self.schedule):
            grad_mode = False
            if task[0] == 0:
                if self.schedule[index+1][0] == 2:      
                    # if next task in schedule is backward  -- no recomputation
                    grad_mode=True

            # For data parallel, sync only when doing last microbatch fwd/bwd
            if self.data_parallel and task[1] < (len(self.batches) - 1):
                with self.model.no_sync():
                    self.worker(task[0], grad_mode, self.batches[task[1]])
            else:
                self.worker(task[0], grad_mode, self.batches[task[1]])
        
        if self.acts_recieve_thread is not None:
            self.acts_recieve_thread.join()
        if self.grads_recieve_thread is not None:
            self.grads_recieve_thread.join()

        if self.acts_send_thread is not None:
            self.acts_send_thread.join()
        if self.grads_send_thread is not None:
            self.grads_send_thread.join()

        # return loss
        return self.average_loss

def scatter(input, chunk_size):
    """
    Accepts input dictionary and splits into microbatches
    """
    # assert(isinstance(inputs,dict) , "varuna inputs must be given as a dictionary")
    
    microbatches = []
    for k,v in input.items():
        # TODO: what will happen for indivisibilities in uneven data parallelism !!
        chunked_values = v.split(chunk_size)
        for i,value in enumerate(chunked_values):
            if len(microbatches) <= i:
                microbatches.append(dict())
            microbatches[i][k]=value
    
    return microbatches
