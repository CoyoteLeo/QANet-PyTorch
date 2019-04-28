import argparse
import os
import random

import math
import numpy as np
import torch
import torch.cuda
import torch.nn as nn
import ujson as json
from torch import optim
from torch.optim.lr_scheduler import LambdaLR
from tqdm import tqdm

import config
from models import QANet
from utils import EMA, Evaluator, SQuADDataset

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


class Runner(object):
    def __init__(self, squad_version, loss):
        self.version = squad_version
        self.dir = os.path.join(config.SQUAD_DIR, squad_version)
        self.loss = loss

    def _train(self, model: nn.Module, optimizer: optim.Adam, scheduler: LambdaLR, ema: EMA, dataset: SQuADDataset,
               start: int, length: int):
        model.train()
        losses = []
        for i in tqdm(range(start, length + start), total=length):
            optimizer.zero_grad()
            Cwid, Ccid, Qwid, Qcid, y1, y2, ids = dataset[i]
            Cwid, Ccid, Qwid, Qcid = Cwid.to(device), Ccid.to(device), Qwid.to(device), Qcid.to(device)
            p1, p2 = model(Cwid, Ccid, Qwid, Qcid)
            y1, y2 = y1.to(device), y2.to(device)
            # TODO: test for cross entropy: raw is nll_loss
            loss1 = self.loss(p1, y1)
            loss2 = self.loss(p2, y2)
            loss = torch.mean(loss1 + loss2)
            loss.backward()
            losses.append(loss.item())
            optimizer.step()
            scheduler.step()
            for name, p in model.named_parameters():
                if p.requires_grad:
                    ema.update_parameter(name, p)
            # torch.nn.utils.clip_grad_norm_(model.parameters(), config.grad_clip)
        loss_avg = np.mean(losses)
        print(f"STEP {format(i + 1, '8d')} loss {format(loss_avg, '.8f')}\n")

    def _test(self, model: nn.Module, dataset: SQuADDataset, eval_file: dict, mode: str = 'test'):
        model.eval()
        answer_dict = {}
        losses = []
        if mode == "test":
            iterator = tqdm(random.sample(range(0, len(dataset)), config.VALIDATION_STEPS),
                            total=config.VALIDATION_STEPS)
        else:
            iterator = tqdm(range(config.TEST_STEPS), total=config.TEST_STEPS)

        evaluator = Evaluator(eval_file=eval_file)
        with torch.no_grad():
            for i in iterator:
                Cwid, Ccid, Qwid, Qcid, y1, y2, ids = dataset[i]
                Cwid, Ccid, Qwid, Qcid = Cwid.to(device), Ccid.to(device), Qwid.to(device), Qcid.to(device)
                p1, p2 = model(Cwid, Ccid, Qwid, Qcid)
                y1, y2 = y1.to(device), y2.to(device)
                loss1 = self.loss(p1, y1)
                loss2 = self.loss(p2, y2)
                loss = torch.mean(loss1 + loss2)
                losses.append(loss.item())

                yp1 = torch.argmax(p1, 1)
                yp2 = torch.argmax(p2, 1)
                yps = torch.stack((yp1, yp2), dim=1)
                ymin, _ = torch.min(yps, 1)
                ymax, _ = torch.max(yps, 1)
                evaluator.update_result(ids.tolist(), ymin.tolist(), ymax.tolist())
        loss = np.mean(losses)
        metrics = evaluator.evaluate()
        metrics["loss"] = loss
        if mode == "test":
            with open(os.path.join(self.dir, "log", "answers.json"), "w") as f:
                json.dump(answer_dict, f)
        print(f"{mode.upper()} loss {format(loss, '.8f')} "
              f"F1 {format(metrics['f1'], '.8f')} "
              f"EM {format(metrics['exact_match'], '.8f')}\n")
        return metrics

    def train(self):
        if os.path.isdir(os.path.join(self.dir, "log")):
            os.makedirs(os.path.join(self.dir, "log"))
        with open(os.path.join(self.dir, config.WORD_EMB_FILE), "r") as f:
            word_mat = torch.tensor(np.array(json.load(f), dtype=np.float32))
        with open(os.path.join(self.dir, config.CHAR_EMB_FILE), "r") as f:
            char_mat = torch.tensor(np.array(json.load(f), dtype=np.float32))
        with open(os.path.join(self.dir, config.TRAIN_EVAL_FILE), "r") as f:
            train_eval_file = json.load(f)
        with open(os.path.join(self.dir, config.DEV_EVAL_FILE), "r") as f:
            dev_eval_file = json.load(f)

        train_dataset = SQuADDataset(os.path.join(self.dir, config.TRAIN_RECORD_FILE), config.STEPS, config.BATCH_SIZE)
        dev_dataset = SQuADDataset(os.path.join(self.dir, config.TRAIN_RECORD_FILE), config.TEST_STEPS,
                                   config.BATCH_SIZE)

        model = QANet(word_mat, char_mat).to(device)
        ema = EMA(config.EMA_DECAY)
        for name, p in model.named_parameters():
            if p.requires_grad:
                ema.set(name, p)

        optimizer = optim.Adam(filter(lambda param: param.requires_grad, model.parameters()),
                               lr=config.LEARNING_RATE, betas=(config.ADAM_BETA1, config.ADAM_BETA2), eps=1e-8,
                               weight_decay=3e-7)
        cr = 1.0 / math.log(config.LEARNING_RATE_WARM_UP_STEPS)
        scheduler = optim.lr_scheduler.LambdaLR(
            optimizer,
            lr_lambda=lambda epoch_number: cr * math.log(epoch_number + 1)
            if epoch_number < config.LEARNING_RATE_WARM_UP_STEPS else config.LEARNING_RATE
        )

        # TODO: raw
        # optimizer = optim.Adam(filter(lambda param: param.requires_grad, model.parameters()),
        #                        lr=config.BASE_LEARNING_RATE, betas=(config.ADAM_BETA1, config.ADAM_BETA2), eps=1e-8,
        #                        weight_decay=3e-7)
        # cr = config.LEARNING_RATE / log2(config.LEARNING_RATE_WARM_UP_STEPS)
        # scheduler = optim.lr_scheduler.LambdaLR(
        #     optimizer,
        #     lr_lambda=lambda epoch_number: cr * log2(epoch_number + 1)
        #     if epoch_number < config.LEARNING_RATE_WARM_UP_STEPS else config.LEARNING_RATE)

        best_f1 = best_em = patience = 0
        for iter in range(0, config.STEPS, config.CHECKPOINT):
            self._train(model=model, optimizer=optimizer, scheduler=scheduler, ema=ema, dataset=train_dataset,
                        start=iter, length=config.CHECKPOINT)
            self._test(model, train_dataset, train_eval_file, mode="validate")
            metrics = self._test(model, dev_dataset, dev_eval_file)
            print("Learning rate: {}".format(scheduler.get_lr()))
            dev_f1 = metrics["f1"]
            dev_em = metrics["exact_match"]
            if dev_f1 < best_f1 and dev_em < best_em:
                patience += 1
                if patience > config.EARLY_STOP: break
            else:
                patience = 0
                best_f1 = max(best_f1, dev_f1)
                best_em = max(best_em, dev_em)

            torch.save(model, os.path.join(self.dir, "model.pt"))

    def test(self):
        with open(os.path.join(self.dir, config.DEV_EVAL_FILE), "r") as f:
            eval_file = json.load(f)
        dataset = SQuADDataset(os.path.join(self.dir, config.DEV_RECORD_FILE), -1, config.BATCH_SIZE)
        model = torch.load(os.path.join(self.dir, "model.pt"))
        self._test(model=model, dataset=dataset, eval_file=eval_file, mode="test")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Preprocess data and generate training and testing example')
    parser.add_argument('--squad-version', default='v1.1', type=str, dest="squad_version",
                        help=f'please check that you have already preprocessing the correspond squad file')
    parser.add_argument("--mode", action="store", dest="mode", default="debug", help="train/test/debug")
    parser = parser.parse_args()
    runner = Runner(squad_version=parser.squad_version, loss=nn.CrossEntropyLoss())
    if parser.mode == "train":
        runner.train()
    elif parser.mode == "debug":
        config.BATCH_SIZE = 1
        config.STEPS = 32
        config.TEST_STEPS = 2
        config.VALIDATION_STEPS = 2
        config.CHECKPOINT = 2
        config.period = 1
        runner.train()
    elif parser.mode == "test":
        runner.test()
    else:
        print("Unknown mode")
        exit(0)
