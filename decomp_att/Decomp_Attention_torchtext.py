from torchtext import data, datasets
from torch.autograd import Variable
import torch
import torch.nn as nn
import torch.nn.functional as F
import re
import random
import argparse
import numpy as np
import sys


# add parameters
parser = argparse.ArgumentParser(description='decomposable_attention')
parser.add_argument('--num_labels', default=3, type=int, help='number of labels (default: 3)')
parser.add_argument('--hidden_dim', default=200, type=int, help='hidden dim (default: 200)')
parser.add_argument('--batch_size', default=64, type=int, help='batch size (default: 32)')
parser.add_argument('--learning_rate', default=0.05, type=float, help='learning rate (default: 0.05)')
parser.add_argument('--embedding_dim', default=100, type=int, help='embedding dim (default: 300)')
parser.add_argument('--para_init', help='parameter initialization gaussian', type=float, default=0.01)
parser.add_argument('--device', help='use GPU', default=None)
parser.add_argument('--encoder', help='save encoder', default='encoder_charngram.pt')
parser.add_argument('--model', help='save model', default='charngram.pt')
args = parser.parse_args()

use_cuda = torch.cuda.is_available()


class EmbedEncoder(nn.Module):

    def __init__(self, input_size, embedding_dim, hidden_dim, para_init):
        super(EmbedEncoder, self).__init__()

        self.embedding_dim = embedding_dim
        self.hidden_dim = hidden_dim
        self.embed = nn.Embedding(input_size, embedding_dim, padding_idx=1)
        self.input_linear = nn.Linear(embedding_dim, hidden_dim, bias=False)
        self.para_init = para_init

        '''initialize parameters'''
        for m in self.modules():
            if isinstance(m, nn.Linear):
                m.weight.data.normal_(0, self.para_init)

    def forward(self, prem, hypo):
        batch_size = prem.size(0)

        prem_emb = self.embed(prem)
        hypo_emb = self.embed(hypo)

        prem_emb = prem_emb.view(-1, self.embedding_dim)
        hypo_emb = hypo_emb.view(-1, self.embedding_dim)

        prem_emb = self.input_linear(prem_emb).view(batch_size, -1, self.hidden_dim)
        hypo_emb = self.input_linear(hypo_emb).view(batch_size, -1, self.hidden_dim)

        return prem_emb, hypo_emb


# Decomposable Attention
class DecomposableAttention(nn.Module):
    # inheriting from nn.Module!

    def __init__(self, hidden_dim, num_labels, para_init):
        super(DecomposableAttention, self).__init__()

        self.hidden_dim = hidden_dim
        self.num_labels = num_labels
        self.dropout = nn.Dropout(p=0.2)
        self.para_init = para_init

        # layer F, G, and H are feed forward nn with ReLu
        self.mlp_F = self.mlp(hidden_dim, hidden_dim)
        self.mlp_G = self.mlp(2 * hidden_dim, hidden_dim)
        self.mlp_H = self.mlp(2 * hidden_dim, hidden_dim)

        # final layer will not use dropout, so defining independently
        self.linear_final = nn.Linear(hidden_dim, num_labels, bias=True)

        '''initialize parameters'''
        for m in self.modules():
            # print m
            if isinstance(m, nn.Linear):
                m.weight.data.normal_(0, self.para_init)
                m.bias.data.normal_(0, self.para_init)

    def mlp(self, input_dim, output_dim):
        '''
        function define a feed forward neural network with ReLu activations
        @input: dimension specifications
        '''
        feed_forward = []
        feed_forward.append(self.dropout)
        feed_forward.append(nn.Linear(input_dim, output_dim, bias=True))
        feed_forward.append(nn.ReLU())
        feed_forward.append(self.dropout)
        feed_forward.append(nn.Linear(output_dim, output_dim, bias=True))
        feed_forward.append(nn.ReLU())
        return nn.Sequential(*feed_forward)

    def forward(self, prem_emb, hypo_emb):

        '''Input layer'''
        len_prem = prem_emb.size(1)
        len_hypo = hypo_emb.size(1)

        '''Attend'''
        f_prem = self.mlp_F(prem_emb.view(-1, self.hidden_dim))
        f_hypo = self.mlp_F(hypo_emb.view(-1, self.hidden_dim))

        f_prem = f_prem.view(-1, len_prem, self.hidden_dim)
        f_hypo = f_hypo.view(-1, len_hypo, self.hidden_dim)

        e_ij = torch.bmm(f_prem, torch.transpose(f_hypo, 1, 2))
        beta_ij = F.softmax(e_ij.view(-1, len_hypo)).view(-1, len_prem, len_hypo)
        beta_i = torch.bmm(beta_ij, hypo_emb)

        e_ji = torch.transpose(e_ij.contiguous(), 1, 2)
        e_ji = e_ji.contiguous()
        alpha_ji = F.softmax(e_ji.view(-1, len_prem)).view(-1, len_hypo, len_prem)
        alpha_j = torch.bmm(alpha_ji, prem_emb)

        '''Compare'''
        concat_1 = torch.cat((prem_emb, beta_i), 2)
        concat_2 = torch.cat((hypo_emb, alpha_j), 2)
        compare_1 = self.mlp_G(concat_1.view(-1, 2 * self.hidden_dim))
        compare_2 = self.mlp_G(concat_2.view(-1, 2 * self.hidden_dim))
        compare_1 = compare_1.view(-1, len_prem, self.hidden_dim)
        compare_2 = compare_2.view(-1, len_hypo, self.hidden_dim)

        '''Aggregate'''
        v_1 = torch.sum(compare_1, 1)
        v_1 = torch.squeeze(v_1, 1)
        v_2 = torch.sum(compare_2, 1)
        v_2 = torch.squeeze(v_2, 1)
        v_concat = torch.cat((v_1, v_2), 1)
        y_pred = self.mlp_H(v_concat)

        '''Final layer'''
        out = F.log_softmax(self.linear_final(y_pred))

        return out


def training_loop(model, input_encoder, loss, optimizer, input_optimizer, train_iter, dev_iter, use_shrinkage):
    step = 0
    best_dev_acc = 0

    while step <= num_train_steps:
        input_encoder.train()
        model.train()

        for batch in train_iter:
            premise = batch.premise.transpose(0, 1)
            hypothesis = batch.hypothesis.transpose(0, 1)
            labels = batch.label - 1
            input_encoder.zero_grad()
            model.zero_grad()

            # initialize the optimizer
            if step == 0:
                for group in input_optimizer.param_groups:
                    for p in group['params']:
                        state = input_optimizer.state[p]
                        state['sum'] += 0
                for group in optimizer.param_groups:
                    for p in group['params']:
                        state = optimizer.state[p]
                        state['sum'] += 0

            if use_cuda:
                prem_emb, hypo_emb = input_encoder(premise.cuda(), hypothesis.cuda())
            else:
                prem_emb, hypo_emb = input_encoder(premise, hypothesis)

            output = model(prem_emb, hypo_emb)

            if use_cuda:
                lossy = loss(output, labels.cuda())
            else:
                lossy = loss(output, labels)

            lossy.backward()

            # Add shinkage
            if use_shrinkage is True:
                grad_norm = 0.
                for m in input_encoder.modules():
                    if isinstance(m, nn.Linear):
                        grad_norm += m.weight.grad.data.norm() ** 2
                        if m.bias is not None:
                            grad_norm += m.bias.grad.data.norm() ** 2
                for m in model.modules():
                    if isinstance(m, nn.Linear):
                        grad_norm += m.weight.grad.data.norm() ** 2
                        if m.bias is not None:
                            grad_norm += m.bias.grad.data.norm() ** 2
                grad_norm ** 0.5
                shrinkage = 5 / (grad_norm + 1e-6)
                if shrinkage < 1:
                    for m in input_encoder.modules():
                        if isinstance(m, nn.Linear):
                            m.weight.grad.data = m.weight.grad.data * shrinkage
                    for m in model.modules():
                        if isinstance(m, nn.Linear):
                            m.weight.grad.data = m.weight.grad.data * shrinkage
                            m.bias.grad.data = m.bias.grad.data * shrinkage

            input_optimizer.step()
            optimizer.step()
            if step % 100 == 0:
                dev_acc = evaluate(model, input_encoder, dev_iter)
                if dev_acc > best_dev_acc:
                    best_dev_acc = dev_acc
                    torch.save(input_encoder.state_dict, args.encoder)
                    torch.save(model.state_dict(), args.model)
                print("Step %i; Loss %f; Dev acc %f; Best dev acc %f;" % (step, lossy.data[0], dev_acc, best_dev_acc))
                sys.stdout.flush()
            if step >= num_train_steps:
                return best_dev_acc
            step += 1


def evaluate(model, input_encoder, data_iter):
    input_encoder.eval()
    model.eval()
    correct = 0
    total = 0
    for batch in data_iter:
        premise = batch.premise.transpose(0, 1)
        hypothesis = batch.hypothesis.transpose(0, 1)
        labels = (batch.label - 1).data

        if use_cuda:
            prem_emb, hypo_emb = input_encoder(premise.cuda(), hypothesis.cuda())
        else:
            prem_emb, hypo_emb = input_encoder(premise, hypothesis)

        output = model(prem_emb, hypo_emb)

        if use_cuda:
            output.cpu()

        _, predicted = torch.max(output.data, 1)
        total += labels.size(0)
        if use_cuda:
            correct += (predicted == labels.cuda()).sum()
        else:
            correct += (predicted == labels).sum()

    input_encoder.train()
    model.train()
    return correct / float(total)


def main():

    # get data
    inputs = datasets.snli.ParsedTextField(lower=True)
    answers = data.Field(sequential=False)

    train, dev, test = datasets.SNLI.splits(inputs, answers)

    # get input embeddings
    inputs.build_vocab(train, vectors='charngram.100d')
    answers.build_vocab(train)

    # global params
    global input_size, num_train_steps, args
    vocab_size = len(inputs.vocab)
    input_size = vocab_size
    num_train_steps = 50000000

    train_iter, dev_iter, test_iter = data.BucketIterator.splits((train, dev, test), batch_size=args.batch_size, device=args.device)

    # Normalize embedding vector (l2-norm = 1)
    word_vecs = inputs.vocab.vectors.numpy()
    word_vecs_normalize = torch.from_numpy((word_vecs.T / (np.linalg.norm(word_vecs, ord=2, axis=1) + np.array([1e-6]) * 100)).T)

    input_encoder = EmbedEncoder(input_size, args.embedding_dim, args.hidden_dim, args.para_init)
    input_encoder.embed.weight.data.copy_(word_vecs_normalize)
    input_encoder.embed.weight.requires_grad = False
    model = DecomposableAttention(args.hidden_dim, args.num_labels, args.para_init)

    if use_cuda:
        input_encoder.cuda()
        model.cuda()

    # Loss
    loss = nn.NLLLoss()

    # Optimizer
    para1 = filter(lambda p: p.requires_grad, input_encoder.parameters())
    para2 = model.parameters()
    input_optimizer = torch.optim.Adagrad(para1, lr=args.learning_rate, weight_decay=5e-5)
    optimizer = torch.optim.Adagrad(para2, lr=args.learning_rate, weight_decay=5e-5)

    # Train the model
    best_dev_acc = training_loop(model, input_encoder, loss, optimizer, input_optimizer, train_iter, dev_iter, use_shrinkage=False)
    print(best_dev_acc)


if __name__ == '__main__':
    main()
