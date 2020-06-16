#! /bin/bash
#export PATH="/home/varuna/anaconda3/bin:$PATH"
#which conda

RANK=0
WORLD_SIZE=2

NNODES=$1
NODE_RANK=$2
MASTER_ADDR=$3
ckpt=$4

echo $NNODES $NODE_RANK $MASTER_ADDR $ckpt
date
ifconfig eth0 | grep inet

DATA_PATH=/home/varuna/bert-large-blob/openwebtext_subset/openwebtext-subset_text_document
CHECKPOINT_PATH=/home/varuna/bert-large-blob/mega1_5b_temp

NCCL_DEBUG=INFO NCCL_SOCKET_IFNAME=eth0 NCCL_SOCKET_NTHREADS=4 NCCL_NSOCKS_PERTHREAD=4 \
python3 run_varuna.py --nstages 10 --batch_size 8192 --chunk_size 4 --total_num_stages 50 \
       --ngpus_per_server 4 --nservers $NNODES --node_rank $NODE_RANK --master_addr $MASTER_ADDR pretrain_gpt2.py \
       --num-layers 48 \
       --hidden-size 1600 \
       --num-attention-heads 16 \
       --seq-length 1024 \
       --max-position-embeddings 1024 \
       --train-iters 31250 \
       --lr-decay-iters 20000 \
       --save $CHECKPOINT_PATH \
       --load $CHECKPOINT_PATH \
       --data-path $DATA_PATH \
       --vocab-file gpt2-vocab.json \
       --merge-file gpt2-merges.txt \
       --data-impl mmap \
       --split 949,50,1 \
       --distributed-backend gloo \
       --lr 0.0024 \
       --min-lr 1e-5 \
       --lr-decay-style cosine \
       --weight-decay 1e-2 \
       --clip-grad 1.0 \
       --warmup .05 \
       --log-interval 1 \
       --save-interval 700 \
       --max-num-ckpts 3 \
       --min-ckpt-iter-to-remove 2970 \
       --load-iteration $ckpt \
       --eval-interval 100 \
       --eval-iters 10 \
       --loss-file varuna_mega_1_5b \
       --fp16 --varuna
# num params = 20,753,384,400
set +x
