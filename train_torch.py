# -*- coding: utf-8 -*-
import argparse
import logging

import numpy as np
import pandas as pd
import os
from datetime import date, datetime, timezone, timedelta
import matplotlib as mpl
import matplotlib.pyplot as plt
import matplotlib.image as img
import torch
from pytorch_lightning import Trainer
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning.core.lightning import LightningModule
from torch.utils.data import DataLoader, Dataset
from transformers.optimization import AdamW, get_cosine_schedule_with_warmup
from transformers import PreTrainedTokenizerFast, GPT2LMHeadModel, AutoTokenizer, AutoModelForSequenceClassification

parser = argparse.ArgumentParser(description='Simsimi based on KoGPT-2')

parser.add_argument('--chat',
                    action='store_true',
                    default=False,
                    help='response generation on given user input')

parser.add_argument('--sentiment',
                    type=str,
                    default='0',
                    help='sentiment for system. 0 is neutral, 1 is negative, 2 is positive.')

parser.add_argument('--model_params',
                    type=str,
                    default='model_chp/model_-last.ckpt',
                    help='model binary for starting chat')

parser.add_argument('--train',
                    action='store_true',
                    default=False,
                    help='for training')

logger = logging.getLogger()
logger.setLevel(logging.INFO)

U_TKN = '<usr>'
S_TKN = '<sys>'
BOS = '</s>'
EOS = '</s>'
MASK = '<unused0>'
SENT = '<unused1>'
PAD = '<pad>'

TOKENIZER = PreTrainedTokenizerFast.from_pretrained("skt/kogpt2-base-v2",
            bos_token=BOS, eos_token=EOS, unk_token='<unk>',
            pad_token=PAD, mask_token=MASK) 

def fix_font():
    # From https://HC.Dle.pw, By Jinseo Kim
    # v1.0.0
    os.system("apt-get install -y fonts-nanum")
    os.system("fc-cache -fv")
    mpl.font_manager._rebuild()
    findfont = mpl.font_manager.fontManager.findfont
    mpl.font_manager.findfont = findfont
    mpl.backends.backend_agg.findfont = findfont
    plt.rcParams['font.family'] = "NanumBarunGothic"
    plt.rcParams['axes.unicode_minus'] = False
           
class CharDataset(Dataset):
    def __init__(self, chats, max_len=32):
        self._data = chats
        self.first = True
        self.q_token = U_TKN
        self.a_token = S_TKN
        self.sent_token = SENT
        self.bos = BOS
        self.eos = EOS
        self.mask = MASK
        self.pad = PAD
        self.max_len = max_len
        self.tokenizer = TOKENIZER 

    def __len__(self):
        return len(self._data)

    def __getitem__(self, idx):
        turn = self._data.iloc[idx]
        q = turn['Q']
        a = turn['A']
        sentiment = str(turn['label'])
        q_toked = self.tokenizer.tokenize(self.q_token + q + \
                                          self.sent_token + sentiment)   
        q_len = len(q_toked)
        a_toked = self.tokenizer.tokenize(self.a_token + a + self.eos)
        a_len = len(a_toked)
        if q_len + a_len > self.max_len:
            a_len = self.max_len - q_len
            if a_len <= 0:
                q_toked = q_toked[-(int(self.max_len/2)):]
                q_len = len(q_toked)
                a_len = self.max_len - q_len
                assert a_len > 0
            a_toked = a_toked[:a_len]
            a_len = len(a_toked)
            assert a_len == len(a_toked), f'{a_len} ==? {len(a_toked)}'
        # [mask, mask, ...., mask, ..., <bos>,..A.. <eos>, <pad>....]
        labels = [
            self.mask,
        ] * q_len + a_toked[1:]
        if self.first:
            logging.info("contexts : {}".format(q))
            logging.info("toked ctx: {}".format(q_toked))
            logging.info("response : {}".format(a))
            logging.info("toked response : {}".format(a_toked))
            logging.info('labels {}'.format(labels))
            self.first = False
        mask = [0] * q_len + [1] * a_len + [0] * (self.max_len - q_len - a_len)
        self.max_len
        labels_ids = self.tokenizer.convert_tokens_to_ids(labels)
        while len(labels_ids) < self.max_len:
            labels_ids += [self.tokenizer.pad_token_id]
        token_ids = self.tokenizer.convert_tokens_to_ids(q_toked + a_toked)
        while len(token_ids) < self.max_len:
            token_ids += [self.tokenizer.pad_token_id]
        return(token_ids, np.array(mask),
               labels_ids)


class KoGPT2Chat(LightningModule):
    def __init__(self, hparams, **kwargs):
        super(KoGPT2Chat, self).__init__()
        self.hparams = hparams
        self.neg = -1e18
        self.kogpt2 = GPT2LMHeadModel.from_pretrained('skt/kogpt2-base-v2')
        self.loss_function = torch.nn.CrossEntropyLoss(reduction='none')

    @staticmethod
    def add_model_specific_args(parent_parser):
        # add model specific args
        parser = argparse.ArgumentParser(parents=[parent_parser], add_help=False)
        parser.add_argument('--max-len',
                            type=int,
                            default=32,
                            help='max sentence length on input (default: 32)')

        parser.add_argument('--batch-size',
                            type=int,
                            default=96,
                            help='batch size for training (default: 96)')
        parser.add_argument('--lr',
                            type=float,
                            default=5e-5,
                            help='The initial learning rate')
        parser.add_argument('--warmup_ratio',
                            type=float,
                            default=0.1,
                            help='warmup ratio')
        return parser

    def forward(self, inputs):
        # (batch, seq_len, hiddens)
        output = self.kogpt2(inputs, return_dict=True)
        return output.logits

    def training_step(self, batch, batch_idx):
        token_ids, mask, label = batch
        out = self(token_ids)
        mask_3d = mask.unsqueeze(dim=2).repeat_interleave(repeats=out.shape[2], dim=2)
        mask_out = torch.where(mask_3d == 1, out, self.neg * torch.ones_like(out))
        loss = self.loss_function(mask_out.transpose(2, 1), label)
        loss_avg = loss.sum() / mask.sum()
        self.log('train_loss', loss_avg)
        return loss_avg

    def configure_optimizers(self):
        # Prepare optimizer
        param_optimizer = list(self.named_parameters())
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
            {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]
        optimizer = AdamW(optimizer_grouped_parameters,
                          lr=self.hparams.lr, correct_bias=False)
        # warm up lr
        num_train_steps = len(self.train_dataloader()) * self.hparams.max_epochs
        num_warmup_steps = int(num_train_steps * self.hparams.warmup_ratio)
        scheduler = get_cosine_schedule_with_warmup(
            optimizer,
            num_warmup_steps=num_warmup_steps, num_training_steps=num_train_steps)
        lr_scheduler = {'scheduler': scheduler, 'name': 'cosine_schedule_with_warmup',
                        'monitor': 'loss', 'interval': 'step',
                        'frequency': 1}
        return [optimizer], [lr_scheduler]

    def _collate_fn(self, batch):
        data = [item[0] for item in batch]
        mask = [item[1] for item in batch]
        label = [item[2] for item in batch]
        return torch.LongTensor(data), torch.LongTensor(mask), torch.LongTensor(label)

    def train_dataloader(self):
        data = pd.read_csv('Chatbot_data/ChatbotData.csv')
        self.train_set = CharDataset(data, max_len=self.hparams.max_len)
        train_dataloader = DataLoader(
            self.train_set, batch_size=self.hparams.batch_size, num_workers=2,
            shuffle=True, collate_fn=self._collate_fn)
        return train_dataloader

    def chat(self, sent='0'):
        fix_font()
        num  = 0
        time_list = []
        emo_list = []
        graph = ShowEmotionGraph()
        tok = TOKENIZER
        sent_tokens = tok.tokenize(sent)
        with torch.no_grad():
            while 1:
                q_temp = input("user > ")
                q = q_temp.strip()
                if q == 'quit':
                    graph.emotion_predict_result(time_list, emo_list, num)
                    break
                a = ''
                KST = timezone(timedelta(hours=9))
                time_record = datetime.now(KST)
                _time = str(time_record.time())[:8]
                
                emo_str, emo_int = graph.sentence_predict(q_temp, num)
                print(emo_str)
                time_list.append(_time)
                emo_list.append(emo_int)
                num += 1
                while 1:
                    input_ids = torch.LongTensor(tok.encode(U_TKN + q + SENT + sent + S_TKN + a)).unsqueeze(dim=0)
                    pred = self(input_ids)
                    gen = tok.convert_ids_to_tokens(
                        torch.argmax(
                            pred,
                            dim=-1).squeeze().numpy().tolist())[-1]
                    if gen == EOS:
                        break
                    a += gen.replace('▁', ' ')
                print("Simsimi > {}".format(a.strip()))

class ShowEmotionGraph():
    def __init__(self):
        self.tokenizer = AutoTokenizer.from_pretrained("beomi/KcELECTRA-base-v2022")
        self.model = AutoModelForSequenceClassification.from_pretrained("JasonJeon/KcElectra_sentiment")
        
    def sentence_predict(self, sent, num):
    # 입력된 문장 토크나이징
        tokenized_sent = self.tokenizer(
            sent,
            return_tensors="pt",
            truncation=True,
            add_special_tokens=True,
            max_length=128
        )
        
        outputs = self.model(
            input_ids = tokenized_sent["input_ids"],
            attention_mask=tokenized_sent["attention_mask"],
            token_type_ids=tokenized_sent["token_type_ids"])
            
    # 결과 return
        logits = outputs[0]
        logits = logits.detach().cpu()
        prob = logits.softmax(dim=1)
        x = ['슬픔', '중립', '행복', '혐오', '분노', '공포', '놀람']
        y = [prob[0][0], prob[0][1], prob[0][2],prob[0][3],prob[0][4],prob[0][5],prob[0][6]]

        bar = plt.bar(x,y,color='slateblue')
        for rect in bar:
            height = rect.get_height()
            plt.text(rect.get_x() + rect.get_width()/2.0, height, '%.1f' % height, ha='center', va='bottom', size = 10)
        plt.title("문장: " + sent)
        plt.legend(['감정'])
        plt.savefig("fig" + str(num) + ".png", dpi=300)
        plt.clf()

        print(prob)
        result = logits.argmax(-1)
        if result == 0:
            result = "슬픔"
        elif result == 1:
            result = "중립"
        elif result == 2:
            result = "행복"
        elif result == 3: 
            result = "혐오"
        elif result == 4:
            result = "분노"
        elif result == 5:
            result = "공포"
        elif result == 6:
            result = "놀람"

        return result, logits.argmax(-1)
    
    def emotion_predict_result(self, x, y, num):
        time_list = x
        emo_list = y
        emo_label = ['슬픔', '중립', '행복', '혐오', '분노', '공포', '놀람']

        fig = plt.figure(figsize=(12, 8))
        ax = fig.add_subplot(1, 1, 1)

        ax.plot(time_list, emo_list, color='red', linewidth=2)

        ax.set_yticks([0, 1, 2, 3, 4, 5, 6])
        ax.set_yticklabels(emo_label, fontsize=12)
        for i in range(len(emo_list)):
            ax.text(0.05 + i,emo_list[i],emo_label[emo_list[i]])
        ax.set_xlabel('시간', fontsize=16)
        ax.set_ylabel('감정 각성도', fontsize=16)
        plt.savefig("fig" + str(num) + ".png", dpi=300)

parser = KoGPT2Chat.add_model_specific_args(parser)
parser = Trainer.add_argparse_args(parser)
args = parser.parse_args()
logging.info(args)

if __name__ == "__main__":
    if args.train:
        checkpoint_callback = ModelCheckpoint(
            dirpath='model_chp',
            filename='{epoch:02d}-{train_loss:.2f}',
            verbose=True,
            save_last=True,
            monitor='train_loss',
            mode='min',
            prefix='model_'
        )
        # python train_torch.py --train --gpus 1 --max_epochs 3
        model = KoGPT2Chat(args)
        model.train()
        trainer = Trainer.from_argparse_args(
            args,
            checkpoint_callback=checkpoint_callback, gradient_clip_val=1.0)
        trainer.fit(model)
        logging.info('best model path {}'.format(checkpoint_callback.best_model_path))
    if args.chat:
        model = KoGPT2Chat.load_from_checkpoint(args.model_params)
        model.chat()
