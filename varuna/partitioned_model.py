import torch
import torch.distributed as dist
from torch.nn import Module

import os, sys
import inspect
import time
import pickle

from .utils import save_rng_states, restore_rng_states, VARUNA_TEMP_FOLDER

from collections import OrderedDict 

class CutPoint(Module):

    def __init__(self):
        super(CutPoint, self).__init__()
        # start with 1 and end before last stage (total num_stages - 1 )
        self.cp_index = -1
        self.cp_func = None
        
        self.set_ret_val_func = None
        self.device = None
        self.send_fn = self.recv_fn = None
        self.stage = -1
        self.num_chunks = -1
        self.num_stages = -1
        self.fp16 = False
        self.pruning = False
        self.barrier_event = None
        self.boundary_func = None
        self.forward_input_shapes = None
        self.dummy_shapes = None
        self.trimmed = False

        self.set_shapes = None
        self.forward_counter = 0
    
    def set_pruning(self, boolean):
        self.pruning = boolean

    def forward(self, *inputs, **kwargs):
        # not set by ModelParallel, pass through as is
        if self.barrier_event is not None:
            self.barrier_event.record()
        if self.boundary_func is not None:
            self.boundary_func()
        
        if self.cp_func is None:
            if len(inputs) == 1:
                return inputs[0]
            return inputs

        if self.trimmed:
            dtype = torch.float16 if self.fp16 else torch.float32
            dummy_inputs = []
            for i,shape in enumerate(self.dummy_shapes):
                dummy_inputs.append(torch.rand(*shape, requires_grad = self.bwd_req_grads[i], dtype=dtype).to(self.device))
            inputs = tuple(dummy_inputs)

        if len(inputs) < 0 or None in inputs:
            if self.pruning:
                inputs = (torch.tensor([-1.0], requires_grad = True))
                inputs = (inputs,)
            else:
                dtype = torch.float16 if self.fp16 else torch.float32
                tensor_inputs = []
                for i in range(len(inputs)):
                    tensor_inputs.append(torch.tensor([-1.0], requires_grad = self.bwd_req_grads[i], dtype=dtype).to(self.device))
                inputs = tuple(tensor_inputs)

        if isinstance(self.cp_func, torch.autograd.Function):
            if self.cp_index == self.stage + 1 and self.stage != self.num_stages-1:
                # New pipeline/iteration: dynamically set shapes of communicated tensors
                if self.forward_counter%(2 * self.num_chunks) == 0:
                    self.forward_counter = 0
                    # my own bwd grad shapes and next gpu's fwd shapes.
                    inp_shapes = [list(i.size()) for i in inputs]
                    self.set_shapes(inp_shapes)

                self.forward_counter += 1
            out = self.cp_func.apply(*inputs)
            if self.cp_index == (self.stage + 1):
                self.set_ret_val_func(out) 
            return out
        
        return self.cp_func(*inputs, **kwargs)

    def set_cp_func(self):
        
        is_in_next_stage = self.cp_index == self.stage
        is_in_prev_stage = self.cp_index == (self.stage + 1)

        class CutpointFunction(torch.autograd.Function):

            @staticmethod
            def forward(ctx, *i):
                # recieve activations
                if is_in_next_stage and self.recv_fn is not None:
                    i = self.recv_fn()
                    if self.trimmed:
                        return torch.rand(1, requires_grad=True).to(self.device) # DUMMY LOSS FOR PROFILING

                # send activations
                elif is_in_prev_stage and self.send_fn is not None:
                    self.send_fn(i)

                if len(i) == 1:
                    return i[0]
                return i

            @staticmethod
            def backward(ctx, *grad_output):
                if self.trimmed:
                    # PyTorch requires that backward function returns same number of values that Forward was called with
                    dummies = []
                    for i,shape in enumerate(self.dummy_shapes):
                        dummies.append(torch.rand(*shape, requires_grad=True).to(self.device))
                    grad_output = tuple(dummies)

                # receive gradients.
                if is_in_prev_stage and self.recv_fn is not None:
                    grad_output = self.recv_fn(grads = True)
                # send gradients
                elif is_in_next_stage and self.send_fn is not None:
                    self.send_fn(grad_output, grads = True)

                if len(grad_output) == 1:
                    return grad_output[0]
                return grad_output

        c = CutpointFunction()
        self.cp_func = c


def dry_run(model, get_batch, from_cache):
    # executes the forward pass of the module on dummy inputs. 
    # Sets the order in which modules are used and the total number of cutpoints declared.

    dummy_inputs = get_batch(1, device='cpu')
    ordered_modules = OrderedDict()
    input_shapes = {}
    input_gradients = {} 
    num_cutpoints = 0

    def get_hook(name):
        def add_module_hook(module, inputs, _output):
            if name not in ordered_modules:
                ordered_modules[name] = module
            if isinstance(module, CutPoint):
                # inputs is a tuple of of tensors
                # input_shapes[name] is a list of each tensor size in that tuple.
                # where the size is itself represented as a list
                input_shapes[name] = [list(i.size()) for i in inputs]
                input_gradients[name] = [i.requires_grad for i in inputs]
        return add_module_hook

    modules = model.named_modules()
    hooks = []

    for name, module in modules:
        if name == "":
            continue
        hooks.append( module.register_forward_hook(get_hook(name)))
        if isinstance(module, CutPoint):
            num_cutpoints += 1
    print("Num cutpoints is", num_cutpoints)
    
    # TODO: do this extra compute on GPU? large models...
    model(**dummy_inputs)
    input_shapes_1 = input_shapes
    input_shapes = dict()
    dummy_inputs_2 = get_batch(2, 'cpu')
    model(**dummy_inputs_2)
    input_shapes_2 = input_shapes
    input_shapes = input_shapes_1

    shape_indices_to_change = dict()
    for name in input_shapes:
        shape_indices_to_change[name] = []

        # iterate through list of tensor-shapes for named cutpoint
        for idx, shape in enumerate(input_shapes_1[name]):
            shape_1 = shape
            shape_2 = input_shapes_2[name][idx]
            assert len(shape_1) == len(shape_2) # same nr of dimensions
            indices_to_change = []
            for i, d1 in enumerate(shape_1): # iterate through each dimension
                d2 = shape_2[i]
                if d1 == 1 and d2 == 2:
                    indices_to_change.append(i) # batch-size index
            shape_indices_to_change[name].append(indices_to_change) # append list of indices of batch-size-sensitive dimensions for tensor at idx

    for h in hooks:
        h.remove()

    # TODO: move to proper temp location
    with open("_tmp_ord_mod",'wb') as f:
        pickle.dump(list(ordered_modules.keys()),f)
    with open("_tmp_inp_shapes",'wb') as f:
        pickle.dump(input_shapes,f)
    with open("_tmp_shape_changes",'wb') as f:
        pickle.dump(shape_indices_to_change,f)
    with open("_tmp_inp_grads",'wb') as f:
        pickle.dump(input_gradients,f)

    return ordered_modules, input_shapes, \
            shape_indices_to_change, input_gradients, num_cutpoints

def read_dry_run_out(model):
    with open("_tmp_ord_mod",'rb') as f:
        ordered_modules_keys = pickle.load(f)

    ordered_modules = OrderedDict()
    for n in ordered_modules_keys:
        path = n.split(".")
        modules = model._modules
        for i in range(len(path) - 1):
            modules = modules[path[i]]._modules
        ordered_modules[n] = modules[path[-1]]

    with open("_tmp_inp_shapes",'rb') as f:
        input_shapes = pickle.load(f)
    with open("_tmp_shape_changes",'rb') as f:
        shape_indices_to_change = pickle.load(f)
    with open("_tmp_inp_grads",'rb') as f:
        input_gradients = pickle.load(f)


    num_cutpoints = len(input_shapes)
    
    return ordered_modules, input_shapes, \
            shape_indices_to_change, input_gradients, num_cutpoints


class PartitionedModel(Module):

    def __init__(self, module, rank, local_rank, device, stage_to_rank_map, fp16, stage_to_cut, chunks, shared_weights=None, profiling_stages=None):
        super(PartitionedModel, self).__init__()
        self.module = module
        self.num_stages = len(stage_to_rank_map)
        self.stage_to_rank_map = stage_to_rank_map
        self.rank = rank
        self.local_rank = local_rank
        self.fp16 = fp16
        self.shared_weights = shared_weights
        self.chunks = chunks

        self.stage_to_cut = stage_to_cut

        self.grads_send_queue = self.acts_send_queue = None
        self.acts_queue = self.grads_queue = None
        self.excp_queue = self.grads_shape_queue = None
        
        if device == "cpu":
            # torch.set_device("cpu")
            self.device = torch.device("cpu")
        else:
            torch.cuda.set_device(device)
            self.device = torch.device("cuda", device)

        self.ret_val = None
        self.pre_cp = None
        self.post_cp = None

        self.stage = -1
        for stage in self.stage_to_rank_map:
            if self.rank in self.stage_to_rank_map[stage]:
                self.stage = stage
                break
        else:
            raise ValueError("Rank " + self.rank + " not found in stage to rank map!")

        if profiling_stages is not None:
            self.profiling_stages = [int(i) for i in profiling_stages.split(',')]
        else:
            self.profiling_stages = None

        self.trimmed = False
        # self.logfile = open("wait_logs" + str(self.rank),"w")

    def initialize(self, get_batch_fn, from_cache=False):
        # print("Initializing partitioned model!")
        start = time.time()
        self.dry_run(get_batch_fn, from_cache)

        if self.stage_to_cut is None:
            cuts_per_stage = int((self.num_cutpoints + 1)/self.num_stages)
            self.stage_to_cut = [i for i in range(0, (cuts_per_stage * self.num_stages), cuts_per_stage)]
        assert len(self.stage_to_cut) == self.num_stages, f"Stage-to-cut mapping: {self.stage_to_cut} must be number-of-stages long: {self.num_stages}!"
        assert self.stage_to_cut[-1] <= self.num_cutpoints, f"The last cut index in the stage-to-cut mapping: {self.stage_to_cut[-1]} must be smaller than total number of cutpoints: {self.num_cutpoints}"
        assert all(self.stage_to_cut[i] < self.stage_to_cut[i+1] for i in range(self.num_stages-1))
        print(f"Stage to cut is: {self.stage_to_cut}")

        if self.shared_weights is not None:
            self.find_shared_weight_stages()
        print("dry run time", time.time() - start)

        if self.profiling_stages:
            print(f"PROFILING MODE; Profiling stages: {self.profiling_stages}")
            if not self.stage in self.profiling_stages:
                self.trimmed = True
            else:
                if self.stage != self.num_stages - 1:
                    upper_cut = self.stage_to_cut[self.stage+1]
                else:
                    upper_cut = self.num_cutpoints

        self.prep_cutpoints()
        self.remove_unused_parameters()
        self.model_pruned = True

    def dry_run(self, get_batch, from_cache):

        if self.local_rank == 0 and not (from_cache and \
            all([os.path.exists(f) for f in ["_tmp_ord_mod","_tmp_inp_shapes","_tmp_shape_changes"]])):

            self.ordered_modules, self.input_shapes, self.shape_indices_to_change, \
                self.input_gradients, self.num_cutpoints = dry_run(self.module, get_batch, from_cache)
            dist.barrier()
        else:
            dist.barrier()
            self.ordered_modules, self.input_shapes, self.shape_indices_to_change, \
                self.input_gradients, self.num_cutpoints = read_dry_run_out(self.module)
            print("Num cutpoints is", self.num_cutpoints)

            
        if self.local_rank == 0 and not (from_cache and os.path.exists("_tmp_pstage_mapping")):
            dummy_inputs = get_batch(1, "cpu")
            # TODO: do we really need these many dry runs?
            self.trace_and_store_param_access(dummy_inputs)
            dist.barrier()
        else:
            dist.barrier()
            with open("_tmp_pstage_mapping", 'rb') as f:
                self.param_name_to_pstage = pickle.load(f)

    def trace_and_store_param_access(self, dummy_inputs):
        param_access = dict()
        for p in self.module.parameters():
            param_access[p] = set()

        self.track_cp = 0
        
        def trace_param_access(frame, event, arg):
            if event != 'call':
                return
            co = frame.f_code
            func_name = co.co_name
            if func_name in ['write','__hash__']:
                return
            arg_info = inspect.getargvalues(frame)
            arg_values = [arg_info.locals[n] for n in arg_info.args]
            for arg in arg_values:
                if isinstance(arg, torch.nn.Parameter):
                    if arg in param_access:
                        param_access[arg].add(self.track_cp)
        
        def boundary_func():
            self.track_cp += 1
        for name, module in self.module.named_modules():
            if isinstance(module, CutPoint):
                module.boundary_func = boundary_func
        sys.settrace(trace_param_access)
        with torch.no_grad():
            self.module(**dummy_inputs)
        sys.settrace(None)
        self.track_cp = None

        for name in self.ordered_modules:
            m = self.ordered_modules[name]
            if isinstance(m, CutPoint):
                m.boundary_func = None

        param_name_to_pstage = dict()
        for n,p in self.module.named_parameters():
            assert len(param_access[p]) < 2, f"Parameter {n} in multiple cuts: {param_access[p]}, mark as two shared parameters?"
            accesed_cps = list(param_access[p])
            if len(accesed_cps) > 0:
                if n not in param_name_to_pstage:
                    param_name_to_pstage[n] = accesed_cps[0]
                assert (param_name_to_pstage[n] == int(accesed_cps[0])), \
                        f"Parameter {n} accesed in cut {accesed_cps[0]} but was created in cut {param_name_to_pstage[n]}!"

        cp_index = 0
        modules = self.ordered_modules
        for name in modules:
            module = modules[name]
            if isinstance(module, CutPoint):
                cp_index += 1
                continue
            for n,p in module.named_parameters(recurse=False):
                full_name = name + '.' + n
                if full_name not in param_name_to_pstage:
                    param_name_to_pstage[full_name] = cp_index

        self.param_name_to_pstage = param_name_to_pstage

        with open("_tmp_pstage_mapping",'wb') as f:
            pickle.dump(self.param_name_to_pstage,f)
        
    
    def find_shared_weight_stages(self):
        # TODO: this method is wrong, do trace thing
        all_shared_weights = []
        for w_pair in self.shared_weights:
            all_shared_weights += [w for w in w_pair]
        curr_stage = 0
        weight_stages = dict()
        for m in self.ordered_modules:
            module = self.ordered_modules[m]
            if isinstance(module, CutPoint):
                curr_stage += 1
                continue
            for w in all_shared_weights:
                param_name = w.split(".")[-1]
                module_name = w[ : -len(param_name)-1]
                if m == module_name and hasattr(module, param_name):
                    weight_stages[w] = curr_stage
                    break
                elif m == module_name:
                    print("Here we have the peculiar case of the missing weight", m, param_name)
                    print(getattr(module,param_name))
        
        for w in all_shared_weights:
            if w not in weight_stages:
                param_name = w.split(".")[-1]
                if hasattr(self.module, param_name):
                    weight_stages[w] = curr_stage

        # cuts_per_stage = (self.num_cutpoints + 1)/ self.num_stages
        stage_to_cut_ranges = []
        for stage, cut in enumerate(self.stage_to_cut):
            if stage == self.num_stages - 1:
                stage_to_cut_ranges.append(range(cut, self.num_cutpoints + 1))
            else:
                stage_to_cut_ranges.append(range(cut, self.stage_to_cut[stage+1]))

        shared_weight_stages = []
        for w_pair in self.shared_weights:
            for w in w_pair:
                assert w in weight_stages, "Shared parameter {} not found in model!".format(w)
                cutpoint_id = weight_stages[w] 
                for s, ranges in enumerate(stage_to_cut_ranges):
                    if cutpoint_id in ranges:
                        weight_stages[w] = s
                        break
                # weight_stages[w] = int(weight_stages[w] // cuts_per_stage)

            shared_weight_stages.append(tuple(weight_stages[w_pair[i]] for i in range(len(w_pair))))
        self.shared_weight_stages = shared_weight_stages


    # """ setting actual cutpoint functions for comunication. """
    def prep_cutpoints(self):

        def attach_meta(cutpoint, index, bwd_req_grads, shapes):
            cutpoint.cp_index = index
            cutpoint.num_stages = self.num_stages
            cutpoint.set_ret_val_func = self.set_ret_val
            cutpoint.stage = self.stage
            cutpoint.device = self.device
            cutpoint.fp16 = self.fp16
            cutpoint.bwd_req_grads = bwd_req_grads
            cutpoint.num_chunks = self.chunks
            cutpoint.num_stages = self.num_stages
            cutpoint.set_shapes = self.set_shapes
            cutpoint.trimmed = self.trimmed
            cutpoint.dummy_shapes = shapes
            cutpoint.set_cp_func()

        # self.cuts_per_stage = (self.num_cutpoints + 1) // self.num_stages

        modules = self.ordered_modules
        index = 1
        assigned_index = 1

        self.forward_input_shapes = []
        self.backward_grad_shapes = []

        for name in modules:
            module = modules[name]
            if name == "":
                continue
            if isinstance(module, CutPoint):
                # if (index % self.cuts_per_stage == 0):
                if (index in self.stage_to_cut):
                    # pre cp
                    if assigned_index == self.stage:
                        self.forward_input_shapes = self.input_shapes[name] # a list of lists of shapes
                        self.fwd_inp_shape_changes = self.shape_indices_to_change[name] # a list of lists of indices
                        self.pre_cp = module
                    # post cp
                    if assigned_index == self.stage + 1:
                        self.backward_grad_shapes = self.input_shapes[name]
                        self.bwd_grad_shape_changes = self.shape_indices_to_change[name]
                        self.post_cp = module
                    attach_meta(module, assigned_index, self.input_gradients[name], self.input_shapes[name])
                    assigned_index += 1  
                index += 1
            # found all relevant cutpoints, break
            if assigned_index == self.num_stages:
                break

    # """ remove unused modules to save memory. """
    def remove_unused_parameters(self):

        if self.trimmed: # BAZI TODO: case where there are consecutive trimmed stages
            if self.stage < self.profiling_stages[0]:
                self.module = self.post_cp
            else:
                self.module = self.pre_cp
            return

        pre_cp_index = self.stage
        post_cp_index = self.stage + 1

        is_used = {}
        used_modules = []
        add_flag = (self.stage == 0)
        
        modules = self.ordered_modules

        for name in modules:
            module = modules[name]
            if name == "":
                continue
            if isinstance(module, CutPoint):
                if (module.cp_index == pre_cp_index or module.cp_index == post_cp_index): 
                    add_flag = not add_flag
            else:
                if add_flag:
                    used_modules.append(name)
                is_used[name] = add_flag

        # any module that is used or has children that are used are needed
        for u in used_modules:
            path = u.split(".")
            key = path[0]
            for i in range(1,len(path)):
                is_used[key] = True
                key = key + "." + path[i]

        for m in is_used:
            if not is_used[m]:
                path = m.split(".")
                modules = self.module._modules
                for i in range(len(path) - 1):
                    modules = modules[path[i]]._modules
                modules[path[-1]] = None
                modules[path[-1]] = PassThroughModule()
                self.ordered_modules[m] = None

        self.check_unused_parameters()

    def parameter_names_to_cuts(self):

        modules = list(self.ordered_modules.keys())

        stage_index = 0
        cp_count = 0
        param_name_to_pstage = dict()
        temp_param_names = []

        for name in modules:
            module = self.ordered_modules[name]
            if name == "" or module is None:
                continue
            if isinstance(module, CutPoint):
                for p in temp_param_names:
                    param_name_to_pstage[p] = stage_index
                temp_param_names = []
                cp_count += 1
                # if cp_count >= self.cuts_per_stage:
                #     break
                stage_index += 1
            else:
                for pname,_ in module.named_parameters(recurse=False):
                    param_name = name + "." + pname
                    temp_param_names.append(param_name)


        # last cutpoint
        # if cp_count < self.cuts_per_stage:
        for p in temp_param_names:
            param_name_to_pstage[p] = stage_index
        # TODO: this is still hard-coded!!! BAZI CHECK
        param_name_to_pstage["lm_head_weight"] = stage_index
            
        return param_name_to_pstage

    def check_unused_parameters(self):

        # start_pstage = self.cuts_per_stage * self.stage
        # end_pstage = self.cuts_per_stage * (self.stage+1)
        start_pstage = self.stage_to_cut[self.stage]
        end_pstage = self.stage_to_cut[self.stage+1] if self.stage < (self.num_stages - 1) else (self.num_cutpoints + 1)

        for n,p in self.module.named_parameters():
            if n not in self.param_name_to_pstage:
                # print(f"{n} not in pstage map")
                continue
            pstage = self.param_name_to_pstage[n]
            if pstage != -1 and (pstage < start_pstage or pstage >= end_pstage):
                # to_remove.append(n)
                path = n.split(".")
                parent = self.module
                for i in range(len(path) - 1):
                    parent = getattr(parent, path[i])
                setattr(parent,path[-1], None)
        
        self.model_pruned = True

    def set_ret_val(self, val):
        self.ret_val = val

    def set_queues(self, acts_send, grad_send, acts_recv, grad_recv, recompute, shapes, excp):
        self.acts_send_queue = acts_send
        self.grads_send_queue = grad_send
        self.acts_queue = acts_recv
        self.grads_queue = grad_recv
        self.recompute_queue = recompute
        self.grads_shape_queue = shapes
        self.excp_queue = excp

    def set_shapes(self, shapes):
        # shapes is a list, not a tensor
        if self.trimmed:
            self.grads_shape_queue.put(self.backward_grad_shapes)
        else:
            self.grads_shape_queue.put(shapes)

    def set_send_fn(self, recompute = False):

        def send(tensor_tuple, grads = False):
            sendlist = []

            if self.trimmed:
                if not grads:
                    shapes = self.backward_grad_shapes
                else:
                    shapes = self.forward_input_shapes
                dtype = torch.float16 if self.fp16 else torch.float32
                for i,shape in enumerate(shapes):
                    dummy = torch.rand(*shape, requires_grad=True, dtype=dtype)
                    sendlist.append(dummy)
            else:
                for tensor in tensor_tuple:
                    sendlist.append(tensor.cpu())
            if grads:
                self.grads_send_queue.put(sendlist)
            else:
                if not recompute:
                    self.acts_send_queue.put(sendlist)

        if self.pre_cp is not None:
            self.pre_cp.send_fn = send
        if self.post_cp is not None:
            self.post_cp.send_fn = send

    def set_recv_fn(self, recompute=False):
        acts = None

        if recompute:
            rng_states, acts = self.recompute_queue.get()
            restore_rng_states(rng_states, self.device)
        elif self.stage > 0:
            while acts is None:
                if not self.excp_queue.empty():
                    e = self.excp_queue.get()
                    raise e
                if not self.acts_queue.empty():
                    acts = self.acts_queue.get()
            # acts is a list of tensors or None
        if self.stage > 0:
            acts = tuple(a.to(self.device) for a in acts)

        def recv(grads = False):
            if grads:
                grds = None
                while grds is None:
                    if not self.excp_queue.empty():
                        e = self.excp_queue.get()
                        raise e
                    if not self.grads_queue.empty():
                        grds = self.grads_queue.get()
                        return tuple(g.to(self.device) for g in grds)
            else:
                return acts
        if self.pre_cp is not None:
            self.pre_cp.recv_fn = recv
        if self.post_cp is not None:
            self.post_cp.recv_fn = recv
        return acts

    def set_recv_acts(self, shape, receive_rank): # BAZI TODO: part of evaluation
        def recv(grads=False):
            x = torch.zeros(shape, dtype=torch.float16 if self.fp16 else torch.float32)
            dist.recv(x, receive_rank)
            return x.to(self.device)
        if self.pre_cp is not None:
            self.pre_cp.recv_fn = recv


    def clear_recv_fn(self):
        if self.pre_cp is not None:
            self.pre_cp.recv_fn = None
        if self.post_cp is not None:
            self.post_cp.recv_fn = None

    def set_recording_events(self):
        self.recording_events = []
        if self.stage == 0:
            self.recording_events.append(torch.cuda.Event(enable_timing=True))
        in_stage = (self.stage == 0)
        for name in self.ordered_modules:
            module = self.ordered_modules[name]
            if isinstance(module, CutPoint):
                if module.cp_index == self.stage:
                    in_stage = True
                if in_stage:
                    event = torch.cuda.Event(enable_timing=True)
                    module.barrier_event = event
                    self.recording_events.append(event)
                if module.cp_index == self.stage + 1:
                    in_stage = False
        if self.stage == self.num_stages - 1:
            self.recording_events.append(torch.cuda.Event(enable_timing=True))

    def clear_recording_events(self):
        for name in self.ordered_modules:
            module = self.ordered_modules[name]
            if isinstance(module, CutPoint):
                module.barrier_event = None

    def elapsed_times(self):
        num_barriers = len(self.recording_events)
        times = []
        for i in range(num_barriers-1):
            times.append(
                self.recording_events[i].elapsed_time(self.recording_events[i+1])
                )
        return times

    def forward(self, inputs_as_dict, recompute=False, save_ctx=False, 
                recording=False, handle_comm=False):
        if save_ctx:
            # if these acts are going to be recomputed
            rng_states = save_rng_states(self.device)

        if recording:
            self.set_recording_events()
            if self.stage == 0:
                self.recording_events[0].record()

        if handle_comm:
            self.set_send_fn(recompute)
            recv_acts = self.set_recv_fn(recompute)
        else:
            self.clear_recv_fn()

        try:
            calc_val = self.module(**inputs_as_dict)
            ret_val = self.ret_val if self.ret_val is not None else calc_val
        except Exception as e:
            if self.ret_val is None:
                print(f"Error occurred on GPU {self.rank}: {str(e)}\n", flush=True, force=True)
                raise e

            ret_val = self.ret_val
        self.ret_val = None

        if recording:
            if self.stage == self.num_stages - 1:
                self.recording_events[-1].record()
            self.clear_recording_events()
        
        if save_ctx:
            if self.stage > 0:
                recv_acts = tuple(r.cpu() for r in recv_acts)
            ctx = (rng_states, recv_acts)
            self.recompute_queue.put(ctx)

        return ret_val


class PassThroughModule(Module):

    def __init__(self):
        super(PassThroughModule, self).__init__()

    def forward(self,*args,**kwargs):
        return None
