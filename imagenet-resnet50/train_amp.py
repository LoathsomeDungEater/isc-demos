#####################################
# Progress Timing and Logging
# ----------------------------
from cycling_utils import TimestampedTimer, MetricsTracker

timer = TimestampedTimer("Imported TimestampedTimer & MetricsTracker")

import argparse
import os
import re
import time
from pathlib import Path
import socket
import json
import subprocess
import torch
import torch.distributed as dist
from torch import nn
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader, default_collate
from torch.utils.tensorboard import SummaryWriter
from torchvision import datasets, models
from torchvision.transforms import v2
from cycling_utils import InterruptableDistributedSampler, atomic_torch_save

timer.report("00_imports")

def get_args_parser(add_help=True):
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--accum", type=float, default=1)
    parser.add_argument("--label-smoothing", type=float, default=0.1)
    parser.add_argument("--random-erase", type=float, default=0.1)
    parser.add_argument("--amp", action='store_true') # Defaults to False

    parser.add_argument("--optim", type=str, choices=['sgd','adamw'], default='sgd')
    parser.add_argument("--lr", type=float, default=0.001)
    parser.add_argument("--momentum", type=float, default=0.9)
    parser.add_argument("--weight-decay", type=float, default=0.0001)

    parser.add_argument("--lr-stepevery", type=str, choices=['epoch','batch'], default='epoch')
    parser.add_argument("--lr-sched", type=str, choices=['cyclic','cosine','step'], default='cosine')
    parser.add_argument("--lr-warmup-epochs", type=int, default=5)
    parser.add_argument("--lr-decay-epochs", type=list, default=[60, 80])
    parser.add_argument("--lr-gamma", type=float, default=0.1)

    parser.add_argument(
        "--data-path", type=Path, default="/mnt/.node1/Open-Datasets/imagenet"
    )
    parser.add_argument("--save-dir", type=Path, required=True)
    parser.add_argument("--save-freq", type=int, default=10)
    parser.add_argument("--log-freq", type=int, default=50)
    parser.add_argument("--tboard-path", type=Path, required=True)
    return parser

def check_model_grads(model, args):
    result = 0.0
    if any([torch.isnan(param.grad).any().item() for param in model.parameters()]):
        result = 1.0
    return result

def is_batchnorm_param(name):
    return re.search(r'\.bn\d+\.', name) is not None

def train_loop(
    model,
    optimizer,
    lr_scheduler,
    scaler,
    loss_fn,
    train_dataloader,
    test_dataloader,
    metrics,
    args,
):
    # Determine starting progress through epoch
    epoch = train_dataloader.sampler.epoch
    train_batches_per_epoch = len(train_dataloader)
    batch = train_dataloader.sampler.progress // train_dataloader.batch_size
    model.train()

    # Setup for logging
    writer = SummaryWriter(log_dir=args.tboard_path)
    host_device_nans = torch.zeros(3, args.world_size, device=args.device_id)
    host_device_nans[:, int(os.environ["RANK"])] = torch.tensor([int(args.host[2:]), args.device_id, 0])

    # Report and just reset timer
    timer.report(
        f"13_train_start - EPOCH [{epoch:,}] TRAIN BATCH [{batch:,} / {train_batches_per_epoch:,}]"
    )

    # Start the timer
    start = time.perf_counter()
    for batch, (inputs, targets) in enumerate(train_dataloader, start=batch):
        torch.cuda.synchronize()
        metrics["sys"].update({"0_gather_batch": time.perf_counter() - start})
        start = time.perf_counter()

        # Move input and targets to device
        inputs, targets = inputs.to(args.device_id, memory_format=torch.channels_last), targets.to(args.device_id)
        torch.cuda.synchronize()
        metrics["sys"].update({"1_data_device": time.perf_counter() - start})
        start = time.perf_counter()

        # Batch setup - epoch starts with batch 0
        is_log_batch = (batch + 1) % args.log_freq == 0
        is_accum_batch = (batch + 1) % args.accum == 0
        is_save_batch = (batch + 1) % args.save_freq == 0
        is_last_batch = (batch + 1) == train_batches_per_epoch
        is_lrstep_batch = True if args.lr_stepevery == "batch" else is_last_batch
        torch.cuda.synchronize()
        metrics["sys"].update({"2_batch_stats": time.perf_counter() - start})
        start = time.perf_counter()

        # Accumulation batch
        if is_accum_batch or is_last_batch:
            with torch.cuda.amp.autocast(enabled=args.amp): # AMP context for forward pass if AMP
                # Forward pass
                predictions = model(inputs)
                torch.cuda.synchronize()
                metrics["sys"].update({"3_forward": time.perf_counter() - start})
                assert (
                    not torch.isnan(predictions).any().item()
                ), f"NAN IN PREDS ON {args.host} GPU {args.device_id}"
                start = time.perf_counter()
                # Compute loss and log to metrics
                loss = loss_fn(predictions, targets)
                # Reduce loss for the purpose of accumulation
                loss = loss / args.accum
                torch.cuda.synchronize()
                metrics["sys"].update({"4_loss": time.perf_counter() - start})
                assert not torch.isnan(
                    loss
                ).item(), f"NAN IN LOSS ON {args.host} GPU {args.device_id}"
                start = time.perf_counter()

            # Accumulate examples seen and loss locally - scale loss back to normal for metrics reporting
            metrics["train"].update({"examples_seen": len(inputs), "loss": loss.item()})
            torch.cuda.synchronize()
            metrics["sys"].update({"5_metrics_update": time.perf_counter() - start})
            start = time.perf_counter()
            # Backpropagation
            scaler.scale(loss).backward()
            torch.cuda.synchronize()
            metrics["sys"].update({"6_backward": time.perf_counter() - start})
            start = time.perf_counter()
            # Check model grads for nans
            host_device_nans[2, int(os.environ["RANK"])] += check_model_grads(model, args)
            torch.cuda.synchronize()
            metrics["sys"].update({"7_grad_nan_check": time.perf_counter() - start})
            start = time.perf_counter()
            # Optimizer step
            scaler.step(optimizer)
            scaler.update()
            torch.cuda.synchronize()
            metrics["sys"].update({"8_opt_step": time.perf_counter() - start})
            start = time.perf_counter()
            # Zero grad
            optimizer.zero_grad()
            torch.cuda.synchronize()
            metrics["sys"].update({"9_zero_grad": time.perf_counter() - start})
            start = time.perf_counter()

        # Non-accumulation batch
        else:
            with model.no_sync(): # No GPU sync on grad accumulation batch
                with torch.cuda.amp.autocast(enabled=args.amp): # Optional context for forward pass
                    # Forward pass
                    predictions = model(inputs)
                    torch.cuda.synchronize()
                    metrics["sys"].update({"3_forward": time.perf_counter() - start})
                    assert (
                        not torch.isnan(predictions).any().item()
                    ), f"NAN IN PREDS ON {args.host} GPU {args.device_id}"
                    start = time.perf_counter()
                    # Compute loss and log to metrics
                    loss = loss_fn(predictions, targets)
                    # Reduce loss for the purpose of accumulation
                    loss = loss / args.accum
                    torch.cuda.synchronize()
                    metrics["sys"].update({"4_loss": time.perf_counter() - start})
                    assert not torch.isnan(
                        loss
                    ).item(), f"NAN IN LOSS ON {args.host} GPU {args.device_id}"
                    start = time.perf_counter()

                # Accumulate examples seen and loss locally
                metrics["train"].update({"examples_seen": len(inputs), "loss": loss.item()})
                torch.cuda.synchronize()
                metrics["sys"].update({"5_metrics_update": time.perf_counter() - start})
                start = time.perf_counter()
                # Backpropagation
                scaler.scale(loss).backward()
                torch.cuda.synchronize()
                metrics["sys"].update({"6_backward": time.perf_counter() - start})
                start = time.perf_counter()
                # Check model grads for nans
                host_device_nans[2, int(os.environ["RANK"])] += check_model_grads(model, args)
                torch.cuda.synchronize()
                metrics["sys"].update({"7_grad_nan_check": time.perf_counter() - start})
                start = time.perf_counter()

        # Logging to tensorboard
        if is_log_batch or is_last_batch:
            metrics["train"].reduce().reset_local()

            if args.is_master:
                total_progress = batch + epoch * train_batches_per_epoch
                examples_seen = metrics["train"].agg["examples_seen"]
                accum_avg_loss = metrics["train"].agg["loss"] * args.accum / examples_seen # scale back up for accum
                writer.add_scalar("Train/avg_loss", accum_avg_loss, total_progress)
                writer.add_scalar(
                    "Train/learn_rate", lr_scheduler.get_last_lr()[0], total_progress
                )
                writer.add_scalar("Train/global_batch", examples_seen, total_progress)

                # Created here on master only, saved a few lines down
                json_payload = {
                    "epoch": epoch,
                    "batch": batch,
                    "train_loss": accum_avg_loss,
                    "train_lr": lr_scheduler.get_last_lr()[0],
                    "reporting_batch": examples_seen,
                }

            # Reset metrics on all ranks
            metrics["train"].end_epoch()

            # Gather device temperature data
            gpu_result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=temperature.gpu",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
            )
            gpu_temps = [float(v) for v in gpu_result.stdout.strip().split()]
            max_gpu_temp = max(gpu_temps)

            # System metrics
            metrics["sys"].reduce().reset_local()  # sum accumulated over the cluster
            dist.reduce(host_device_nans, dst=0)

            if args.is_master:
                total_duration = 0.0
                for step, duration in metrics["sys"].agg.items():
                    writer.add_scalar(
                        f"Sys/{step}",
                        duration / (args.world_size * args.log_freq),
                        total_progress,
                    )
                    # Exempt nan-checking, logging, and checkpointing time from total_duration
                    if step not in [
                        "7_grad_nan_check",
                        "10_tb_logging",
                        "12_checkpoint_saving",
                    ]:
                        total_duration += duration / (args.world_size * args.log_freq)
                    # Add to json_payload as well
                    json_payload[step] = duration / (args.world_size * args.log_freq)
                writer.add_scalar("Sys/Total_time", total_duration, total_progress)
                writer.add_scalar("Sys/Max_GPU_temp", max_gpu_temp, total_progress)
                writer.add_scalar("Sys/Grad_NaNs", host_device_nans[2,:].sum().item(), total_progress)

                json_payload["total_time"] = total_duration
                json_payload["gpu_temps"] = gpu_temps
                json_payload["host_device_nans"] = host_device_nans.tolist()
                json_payload["datetime"] = time.strftime("%Y-%m-%d %H:%M:%S")

                timer.report(
                    f"EP [{epoch}] TR BA [{batch:,} / {train_batches_per_epoch:,}] "
                    f"LS [{accum_avg_loss:.3f}] S/it [{total_duration:.3f}] IT/s [{1/total_duration:.2f}]"
                )

                # Dump log to json
                with open(os.path.join(args.save_dir, "train_metrics.jsonl"), "a") as f:
                    f.write(json.dumps(json_payload) + "\n")

            metrics["sys"].end_epoch()
            host_device_nans = torch.zeros(3, args.world_size, device=args.device_id)
            host_device_nans[:, int(os.environ["RANK"])] = torch.tensor([int(args.host[2:]), args.device_id, 0])

        torch.cuda.synchronize()
        dist.barrier()  # Add after master-only op to ensure fair timing
        metrics["sys"].update({"10_tb_logging": time.perf_counter() - start})
        start = time.perf_counter()

        # Advance sampler and step scheduler
        train_dataloader.sampler.advance(len(inputs))
        if is_lrstep_batch:
            lr_scheduler.step()
        torch.cuda.synchronize()
        metrics["sys"].update({"11_sampler_lrschd_adv": time.perf_counter() - start})
        start = time.perf_counter()

        # Saving
        if (is_save_batch or is_last_batch) and args.is_master:
            # Save checkpoint
            save_payload = {
                "model": model.state_dict(),
                "optimizer": optimizer.state_dict(),
                "train_sampler": train_dataloader.sampler.state_dict(),
                "test_sampler": test_dataloader.sampler.state_dict(),
                "lr_scheduler": lr_scheduler.state_dict(),
                "scaler": scaler.state_dict(),
                "metrics_train": metrics["train"].state_dict(),
                "metrics_test": metrics["test"].state_dict(),
                "metrics_sys": metrics["sys"].state_dict(),
            }
            atomic_torch_save(
                save_payload, args.checkpoint_path,
            )

        torch.cuda.synchronize()
        dist.barrier()  # Add after master-only op to ensure fair timing
        metrics["sys"].update({"12_checkpoint_saving": time.perf_counter() - start})
        start = time.perf_counter()


def test_loop(
    model,
    optimizer,
    lr_scheduler,
    scaler,
    loss_fn,
    train_dataloader,
    test_dataloader,
    metrics,
    args,
):
    epoch = test_dataloader.sampler.epoch
    test_batches_per_epoch = len(test_dataloader)
    batch = test_dataloader.sampler.progress // test_dataloader.batch_size
    model.eval()

    # Report and just reset timer
    timer.report(
        f"14_test_start - EPOCH [{epoch:,}] TEST BATCH [{batch:,} / {test_batches_per_epoch:,}]"
    )

    # Write setup timing data to tensorboard
    writer = SummaryWriter(log_dir=args.tboard_path)

    with torch.no_grad():
        for batch, (inputs, targets) in enumerate(test_dataloader, start=batch):
            # Batch setup
            batch = test_dataloader.sampler.progress // test_dataloader.batch_size
            is_log_batch = (batch + 1) % args.log_freq == 0
            is_save_batch = (batch + 1) % args.save_freq == 0
            is_last_batch = (batch + 1) == test_batches_per_epoch
            # Move input and targets to device
            inputs, targets = inputs.to(args.device_id), targets.to(args.device_id)
            # Inference
            predictions = model(inputs)
            # Test loss
            test_loss = loss_fn(predictions, targets)
            # Advance sampler
            test_dataloader.sampler.advance(len(inputs))
            # Performance metrics logging
            correct = (predictions.argmax(1) == targets).type(torch.float).sum()
            # Gather results from all nodes - sums metrics from all nodes into local aggregate
            metrics["test"].update(
                {
                    "examples_seen": len(inputs),
                    "loss": test_loss.item(),
                    "correct": correct.item(),
                }
            ).reduce().reset_local()

            # Performance summary at the end of the epoch
            if args.is_master and is_last_batch:
                examples_seen = metrics["test"].agg["examples_seen"]
                avg_test_loss = metrics["test"].agg["loss"] / examples_seen
                pct_test_correct = metrics["test"].agg["correct"] / examples_seen
                writer.add_scalar("Test/avg_test_loss", avg_test_loss, epoch)
                writer.add_scalar("Test/pct_test_correct", pct_test_correct, epoch)
                metrics["test"].end_epoch()
                print(f"EPOCH_RESULT :: EPOCH {epoch:,} :: ACC {pct_test_correct:,.4f}")

                # Dump log to json
                json_payload = {
                    "epoch": epoch,
                    "test_loss": avg_test_loss,
                    "test_accu": pct_test_correct,
                    "datetime": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                with open(os.path.join(args.save_dir, "test_metrics.jsonl"), "a") as f:
                    f.write(json.dumps(json_payload) + "\n")

            if is_log_batch and not is_last_batch:
                timer.report(
                    f"EPOCH [{epoch:,}] VA BA [{batch:,} / {test_batches_per_epoch:,}] COMPLETED"
                )

            # Save checkpoint
            if args.is_master and (is_save_batch or is_last_batch):
                # Save checkpoint
                save_payload = {
                    "model": model.state_dict(),
                    "optimizer": optimizer.state_dict(),
                    "train_sampler": train_dataloader.sampler.state_dict(),
                    "test_sampler": test_dataloader.sampler.state_dict(),
                    "lr_scheduler": lr_scheduler.state_dict(),
                    "scaler": scaler.state_dict(),
                    "metrics_train": metrics["train"].state_dict(),
                    "metrics_test": metrics["test"].state_dict(),
                    "metrics_sys": metrics["sys"].state_dict(),
                }
                atomic_torch_save(
                    save_payload, args.checkpoint_path,
                )


def main(args, timer):
    dist.init_process_group("nccl")  # Expects RANK set in environment variable
    args.host = socket.gethostname()
    rank = int(os.environ["RANK"])
    args.device_id = int(os.environ["LOCAL_RANK"])
    args.world_size = int(os.environ["WORLD_SIZE"])
    args.is_master = rank == 0  # Master node for saving / reporting
    torch.cuda.set_device(args.device_id)  # Enables calling 'cuda'
    timer.report(f"03_init_nccl - HOST: {args.host}, WORLD_SIZE {args.world_size}")

    ## NOTE: GRAD SCALER CAUSES NANS - EXPECTED. LOG NANS WITHOUT RAISING
    # torch.autograd.set_detect_anomaly(True) 

    args.checkpoint_path = args.save_dir / "checkpoint.pt"
    args.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    timer.report("04_checkpoint_path")

    ##############################################
    # Data Transformation and Augmentation
    # ----------------------
    img_mean = (0.485, 0.456, 0.406)  # Pytorch official ImageNet
    img_std = (0.229, 0.224, 0.225)  # Pytorch official ImageNet
    train_transform = v2.Compose(
        [
            v2.PILToTensor(),
            v2.RandomResizedCrop(224, antialias=True),
            v2.RandomHorizontalFlip(p=0.5),
            v2.ToDtype(torch.float32, scale=True),  # to float32 in [0, 1]
            v2.Normalize(mean=img_mean, std=img_std),
            # v2.RandomErasing(p=args.random_erase),
            v2.RandAugment()
        ]
    )
    test_transform = v2.Compose(
        [
            v2.PILToTensor(),
            v2.Resize(256, antialias=True),
            v2.CenterCrop(224),
            # v2.Resize(size=(224, 224), antialias=True),
            v2.ToDtype(torch.float32, scale=True),  # to float32 in [0, 1]
            v2.Normalize(mean=img_mean, std=img_std),
        ]
    )

    train_path = os.path.join(args.data_path, "ILSVRC/Data/CLS-LOC/train")
    val_path = os.path.join(args.data_path, "ILSVRC/Data/CLS-LOC/val")
    train_data = datasets.ImageFolder(train_path, transform=train_transform)
    test_data = datasets.ImageFolder(val_path, transform=test_transform)
    timer.report(f"05_init_datasets: found {len(train_data):,} training and {len(test_data):,} test samples.")

    ##############################################
    # Data Samplers and Loaders
    # ----------------------
    train_sampler = InterruptableDistributedSampler(train_data)
    test_sampler = InterruptableDistributedSampler(test_data)
    timer.report("06_init_samplers")

    # Implementing CutMix
    cutmix = v2.CutMix(num_classes=len(train_data.classes))
    mixup = v2.MixUp(num_classes=len(train_data.classes))
    cutmix_or_mixup = v2.RandomChoice([cutmix, mixup])
    def collate_fn(batch):
        return cutmix_or_mixup(*default_collate(batch))

    train_dataloader = DataLoader(
        train_data, batch_size=args.batch_size, sampler=train_sampler, num_workers=3, collate_fn=collate_fn
    )
    test_dataloader = DataLoader(
        test_data, batch_size=args.batch_size, sampler=test_sampler
    )
    timer.report(f"07_init_dataloaders: assembled {len(train_dataloader):,} training and {len(test_dataloader):,} test batches.")

    ##############################################
    # Model Preparation
    # ----------------------
    model = models.resnet50()
    timer.report("08_model_build")
    model = model.to(args.device_id, memory_format=torch.channels_last)
    timer.report("09_model_gpu")
    model = DDP(model, device_ids=[args.device_id])
    timer.report("10_model_ddp")

    ########################################
    # Loss function, Optimizer, Learning rate scheduler
    # ----------------------
    loss_fn = nn.CrossEntropyLoss(reduction="sum", label_smoothing=args.label_smoothing)
    
    param_groups = [{'params': param, 'weight_decay': 0.00} if is_batchnorm_param(name) else {'params': param} for name,param in model.named_parameters()]
    if args.optim  == 'sgd':
        optimizer = torch.optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    elif args.optim == 'adamw':
        optimizer = torch.optim.AdamW(param_groups, lr=args.lr, weight_decay=args.weight_decay)
    else:
        raise Exception("Arg 'optim' must be either 'sgd' or 'adamw'.")
    scaler = torch.cuda.amp.GradScaler(enabled=args.amp) # Enabled or not based on arg 'amp'

    if args.lr_sched == 'cyclic':
        warmup_lambda = lambda epoch: (epoch + 1) / (2 * args.lr_warmup_epochs)
        warmup_scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=warmup_lambda)
        lr_scheduler = torch.optim.lr_scheduler.CyclicLR(
            optimizer, base_lr=args.lr / 2, max_lr=args.lr, step_size_up=5, step_size_down=5, 
            mode='exp_range', gamma=0.98 , cycle_momentum=False
        )
    elif args.lr_sched == 'cosine':
        warmup_lambda = lambda epoch: (epoch + 1) / args.lr_warmup_epochs
        warmup_scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=warmup_lambda)
        lr_scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
            optimizer, T_max=args.epochs, eta_min= 0.01 * args.lr
        )
    elif args.lr_sched == 'step':
        warmup_lambda = lambda epoch: (epoch + 1) / args.lr_warmup_epochs
        warmup_scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=warmup_lambda)
        lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(
            optimizer, milestones=args.lr_decay_epochs, gamma=args.lr_gamma
        )

    lr_scheduler = torch.optim.lr_scheduler.SequentialLR(
        optimizer, schedulers=[warmup_scheduler, lr_scheduler], milestones=[args.lr_warmup_epochs - 1]
    )

    metrics = {
        "train": MetricsTracker(),
        "test": MetricsTracker(),
        "sys": MetricsTracker(),
    }
    timer.report("11_loss_opt_lrsch_met")

    #####################################
    # Retrieve the checkpoint if the experiment is resuming from pause
    # ----------------------
    if os.path.isfile(args.checkpoint_path):
        if args.is_master:
            print(f"Loading checkpoint from {args.checkpoint_path}")
        checkpoint = torch.load(
            args.checkpoint_path, map_location=f"cuda:{args.device_id}"
        )

        model.load_state_dict(checkpoint["model"])
        optimizer.load_state_dict(checkpoint["optimizer"])
        train_dataloader.sampler.load_state_dict(checkpoint["train_sampler"])
        test_dataloader.sampler.load_state_dict(checkpoint["test_sampler"])
        lr_scheduler.load_state_dict(checkpoint["lr_scheduler"])
        scaler.load_state_dict(checkpoint["scaler"])
        metrics["train"].load_state_dict(checkpoint["metrics_train"])
        metrics["test"].load_state_dict(checkpoint["metrics_test"])
        metrics["sys"].load_state_dict(checkpoint["metrics_sys"])

    timer.report("12_load_checkpoint")

    #####################################
    # Main training loop
    # --------------------
    for epoch in range(train_dataloader.sampler.epoch, args.epochs):
        with train_dataloader.sampler.in_epoch(epoch):
            train_loop(
                model,
                optimizer,
                lr_scheduler,
                scaler,
                loss_fn,
                train_dataloader,
                test_dataloader,
                metrics,
                args,
            )

            with test_dataloader.sampler.in_epoch(epoch):
                test_loop(
                    model,
                    optimizer,
                    lr_scheduler,
                    scaler,
                    loss_fn,
                    train_dataloader,
                    test_dataloader,
                    metrics,
                    args,
                )


timer.report("01_define_functions")

if __name__ == "__main__":
    args = get_args_parser().parse_args()
    if int(os.environ["RANK"]) == 0:
        print(f"ARGS: {args}")
    timer.report("02_parse_args")

    main(args, timer)
