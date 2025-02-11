# called by transforers.train.py
import torch
import torch.nn as nn
from transflate.main import make_model
from transflate.training.LabelSmoothing import LabelSmoothing
from transflate.data.dataloader import create_dataloaders
from transflate.training.lr import rate
from transflate.training.TrainState import TrainState
from transflate.training.run_epoch import run_epoch
from transflate.data.Batch import Batch
from transflate.training.SimpleLossCompute import SimpleLossCompute
from transflate.helper import DummyOptimizer, DummyScheduler

import GPUtil   # print GPU memory info

# Packages for distributed computation
# import torch.distributed as dist
# from torch.nn.parallel import DistributedDataParallel as DDP

def train_worker(gpu, ngpus_per_node, vocab_src, vocab_tgt,
    spacy_de, spacy_en, config, architecture, is_distributed=False):
    """
    config : dict with training configurations
    architecture : dict with model configurations

    1. make_model
    2. define criterion
    3. load data loaders: train & valid
    define optimizer
    define lr_scheduler
    init TrainState()
    """

    print(f'Train worker process using GPU n.{gpu}', flush=True)
    torch.cuda.set_device(gpu)

    pad_idx = vocab_tgt["<blank>"]
    model = make_model(
        src_vocab_len = architecture['src_vocab_len'], 
        tgt_vocab_len = architecture['tgt_vocab_len'], 
        N=6,
        d_model=architecture['d_model'], 
        d_ff = architecture['d_ff'],
        h = architecture['h'],
        dropout=architecture['p_dropout'])

    model.cuda(gpu)
    module = model
    is_main_process = True
    # if is_distributed:
    #     dist.init_process_group("nccl", init_method="env://", rank=gpu,world_size=ngpus_per_node)
    #     model = DDP(model, device_ids=[gpu])
    #     module = model.module
    #     is_main_process = gpu == 0

    criterion = LabelSmoothing(size=len(vocab_tgt),
    padding_idx=pad_idx, smoothing=0.1)
    # criterion = nn.KLDivLoss(reduction="sum")
    criterion.cuda(gpu)

    train_dataloader, valid_dataloader = create_dataloaders(
        device=gpu,
        vocab_src=vocab_src,
        vocab_tgt=vocab_tgt,
        spacy_de=spacy_de,
        spacy_en=spacy_en,
        batch_size=config['batch_size'] // ngpus_per_node,
        max_padding=config['max_padding'],
        # is_distributed=is_distributed,
        train=True,
    )

    optimizer = torch.optim.Adam(
        params=model.parameters(),
        lr=config['base_lr'],
        betas=(0.9, 0.98),
        eps=1e-9
    )
    lr_scheduler = torch.optim.lr_scheduler.LambdaLR(
        optimizer=optimizer,
        lr_lambda=lambda step: rate(
            step=step, model_size=architecture['d_model'], factor=1, warmup=config['warmup']
        )
    )
    train_state = TrainState()

    for epoch in range(config['num_epochs']):
        # if is_distributed:
        #     train_dataloader.sampler.set_epoch(epoch)
        #     valid_dataloader.sampler.set_epoch(epoch)

        model.train()
        print(f'[GPU n.{gpu}] Epoch {epoch} Training ====', flush=True)
        _, train_state = run_epoch(
            data_iter=(Batch(src=b[0], tgt=b[1], pad=pad_idx) for b in train_dataloader),
            model=model,
            loss_compute=SimpleLossCompute(generator=module.generator, criterion=criterion),
            optimizer=optimizer,
            scheduler=lr_scheduler,
            mode='train+log',
            accum_iter=config['accum_iter'],
            train_state=train_state,
        )
        GPUtil.showUtilization()
        if is_main_process:
            file_path = f"{config['file_prefix']}.{epoch}.pt"
            torch.save(module.state_dict(), file_path)
        torch.cuda.empty_cache()

        print(f"[GPU n.{gpu}] Epoch {epoch} Validation ===", flush=True)
        model.eval()

        sloss = run_epoch(
            data_iter=(Batch(src=b[0], tgt=b[1], pad=pad_idx) for b in valid_dataloader),
            model=model,
            loss_compute=SimpleLossCompute(generator=module.generator, criterion=criterion),
            optimizer=DummyOptimizer(),
            scheduler=DummyScheduler(),
            mode='eval',
        )
        print(sloss)
        torch.cuda.empty_cache()

    if is_main_process:
        file_path = f"{config['file_prefix']}final.pt"
        torch.save(module.state_dict(), file_path)
        
    return model