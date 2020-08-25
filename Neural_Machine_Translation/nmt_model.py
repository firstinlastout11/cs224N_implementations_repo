from collections import namedtuple
import sys
from typing import List, Tuple, Dict, Set, Union
import torch
import torch.nn as nn
import torch.nn.utils
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_packed_sequence, pack_padded_sequence

from model_embeddings import ModelEmbeddings
Hypothesis = namedtuple('Hypothesis', ['value', 'score'])

class NMT(nn.Module):
    """ Simple Neural Machine Translation Model:
        - Bidrectional LSTM Encoder
        - Unidirection LSTM Decoder
        - Global Attention Model (Luong, et al. 2015)
    """
    def __init__(self, embed_size, hidden_size, vocab, dropout_rate=0.2):
        """ Init NMT Model.

        @param embed_size (int): Embedding size (dimensionality)
        @param hidden_size (int): Hidden Size (dimensionality)
        @param vocab (Vocab): Vocabulary object containing src and tgt languages
                              See vocab.py for documentation.
        @param dropout_rate (float): Dropout probability, for attention
        """

        super(NMT, self).__init__()
        self.embed_size = embed_size
        self.model_embeddings = ModelEmbeddings(embed_size, vocab)
        self.hidden_size = hidden_size
        self.dropout_rate = dropout_rate
        self.vocab = vocab


        # Initiazlie the encoder variable
        # Bidirectional LSTM with bias
        self.encoder = nn.LSTM(input_size = self.embed_size, hidden_size = self.hidden_size, bidirectional = True, bias = True)

        # Initialize the decoder variable (LSTM cell with Bias)
        # input size is the embedding_size + attention output from the previous step
        self.decoder = nn.LSTMCell(input_size = self.embed_size + self.hidden_size, hidden_size = self.hidden_size, bias = True)

        # Initiazlie the linear layers for the linear projections
        self.h_projection = nn.Linear(2 * self.hidden_size, self.hidden_size, bias = False)
        self.c_projection = nn.Linear(2 * self.hidden_size, self.hidden_size, bias = False)

        # Initialize the attention_projection layer
        # This is to compute the attention scores (Default is dot product)
        self.att_projection = nn.Linear(2 * self.hidden_size , self.hidden_size, bias = False)

        # Initialize the outputprojection layer
        # This is to compoute the output vector by combining attention output and decode hidden state
        self.combined_output_projection = nn.Linear(3 * self.hidden_size, self.hidden_size, bias = False)

        # Initialize the target vocab linear layer
        self.target_vocab_projection = nn.Linear(self.hidden_size, len(self.vocab.tgt), bias = False)

        # Initialize dropout
        self.dropout = nn.Dropout(p = self.dropout_rate, inplace = False)
    
    def forward(self, source: List[List[str]], target: List[List[str]]) -> torch.Tensor:
        """ Take a mini-batch of source and target sentences, compute the log-likelihood of
        target sentences under the language models learned by the NMT system.

        @param source (List[List[str]]): list of source sentence tokens
        @param target (List[List[str]]): list of target sentence tokens, wrapped by `<s>` and `</s>`

        @returns scores (Tensor): a variable/tensor of shape (b, ) representing the
                                    log-likelihood of generating the gold-standard target sentence for
                                    each example in the input batch. Here b = batch size.
        """

        # Compute sentence lengths
        source_lengths = [len(s) for s in source]

        # Convert list of lists into tensors
        # Tensor: (src_len, b)
        source_padded = self.vocab.src.to_input_tensor(source, device = self.device)
        # Tensor: (tgt_len, b)
        target_padded = self.vocab.tgt.to_input_tensor(target, device = self.device)

 
        ###     Run the network forward:
        ###     1. Apply the encoder to `source_padded` by calling `self.encode()`
        ###     2. Generate sentence masks for `source_padded` by calling `self.generate_sent_masks()`
        ###     3. Apply the decoder to compute combined-output by calling `self.decode()`
        ###     4. Compute log probability distribution over the target vocabulary using the
        ###        combined_outputs returned by the `self.decode()` function.

        # Apply the encoder to `source_padded` by calling `self.encode()`
        enc_hiddens, dec_init_state = self.encode(source_padded, source_lengths)

        # Generate sentence masks for `source_padded` by calling `self.generate_sent_masks()`
        enc_masks = self.generate_sent_masks(enc_hiddens, source_lengths)

        # Apply the decoder to compute combined-output by calling `self.decode()`
        combined_outputs = self.decode(enc_hiddens, enc_masks, dec_init_state, target_padded)

        # Compute log prob dist. over the target vocab using the combined_outputs
        P = F.log_softmax(self.target_vocab_projection(combined_outputs), dim = 1)

        # Zero out, probabilities for which we have nothing in the target text
        target_masks = (target_padded != self.vocab.tgt['<pad>']).float()

        # Compute log probability of generating true target words
        target_gold_words_log_prob = torch.gather(P, index=target_padded[1:].unsqueeze(-1), dim=-1).squeeze(-1) * target_masks[1:]
        scores = target_gold_words_log_prob.sum(dim=0)
        return scores



    def encode(self, source_padded: torch.Tensor, source_lengths: List[int]) -> Tuple[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        """ Apply the encoder to source sentences to obtain encoder hidden states.
            Additionally, take the final states of the encoder and project them to obtain initial states for decoder.

        @param source_padded (Tensor): Tensor of padded source sentences with shape (src_len, b), where
                                        b = batch_size, src_len = maximum source sentence length. Note that 
                                       these have already been sorted in order of longest to shortest sentence.
        @param source_lengths (List[int]): List of actual lengths for each of the source sentences in the batch
        @returns enc_hiddens (Tensor): Tensor of hidden units with shape (b, src_len, h*2), where
                                        b = batch size, src_len = maximum source sentence length, h = hidden size.
        @returns dec_init_state (tuple(Tensor, Tensor)): Tuple of tensors representing the decoder's initial
                                                hidden state and cell.
        """
        # Construct Tensor `X` of source sentences with shape (src_len, b, e)
        X = self.model_embeddings.source(source_padded)
        
        # Insert X into the encode to compute the outputs, hidden_states, cell_states
        # output of shape (seq_len, batch, num_directions * hidden_size) -> (batch, seq_len, num_directions * hidden_size)
        enc_hiddens, [last_hidden, last_cell] = self.encoder(pack_padded_sequence(X, lengths = source_lengths))
        enc_hiddens, _ = pad_packed_sequence(enc_hiddens, batch_first = True)

        # h_n of shape (num_layers * num_directions, batch, hidden_size)
        # c_n of shape (num_layers * num_directions, batch, hidden_size)
        
        # Compute `dec_init_state` = (init_decoder_hidden, init_decoder_cell):
        init_decoder_hidden = self.h_projection(torch.cat((last_hidden[0], last_hidden[1]), dim = 1))
        init_decoder_cell = self.c_projection(torch.cat((last_cell[0], last_cell[1]), dim = 1))

        dec_init_state = (init_decoder_hidden, init_decoder_cell)

        return enc_hiddens, dec_init_state

    def decode(self, enc_hiddens: torch.Tensor, enc_masks: torch.Tensor,
                dec_init_state: Tuple[torch.Tensor, torch.Tensor], target_padded: torch.Tensor) -> torch.Tensor:
        """Compute combined output vectors for a batch.

        @param enc_hiddens (Tensor): Hidden states (b, src_len, h*2), where
                                     b = batch size, src_len = maximum source sentence length, h = hidden size.
        @param enc_masks (Tensor): Tensor of sentence masks (b, src_len), where
                                     b = batch size, src_len = maximum source sentence length.
        @param dec_init_state (tuple(Tensor, Tensor)): Initial state and cell for decoder
        @param target_padded (Tensor): Gold-standard padded target sentences (tgt_len, b), where
                                       tgt_len = maximum target sentence length, b = batch size. 

        @returns combined_outputs (Tensor): combined output tensor  (tgt_len, b,  h), where
                                        tgt_len = maximum target sentence length, b = batch_size,  h = hidden size
        """

        # Chop off the <END> token for max length sentences
        target_padded = target_padded[:-1]

        # Initialize the decoder state (hidden and cell)
        dec_state = dec_init_state

        # Initialize the previous combined output vector o_{t-1} as zero
        batch_size = enc_hiddens.size(0)
        o_prev = torch.zeros(batch_size, self.hidden_size, device = self.device)

        # Initialize a list we will use to collect the combined output o_t on each step
        combined_outputs = []

        # Apply the attention projection layer to `enc_hiddens` to obtain `enc_hiddens_proj`
        enc_hiddens_proj = self.att_projection(enc_hiddens)

        # Create the tensor Y of target setnences
        Y = self.model_embeddings.target(target_padded)

        # Since the decoder is the LSTMCell, we need to iterate thru each timestep for the computation
        # It's more flexible than LSTM because we can manually compute the computation in each cell
        # Thus, it's appropriate for the attention model

        for Y_t in torch.split(Y, 1, dim = 0):

            # Squeeze the tensor
            Y_t = torch.squeeze(Y_t)

            # Concatenate Y_t with o_prev
            # to compute the input vector for the decoder at timestep t,
            # we concat the original input for decoder (Y_t) and the combined output vector of the previous timestep
            Ybar_t = torch.cat((Y_t, o_prev), dim = 1)

            # Use the step function to compute the decoder's next (cell, state) values
            # We only use the decoder's output vector as an input to the next cell
            _, o_t, _ = self.step(Ybar_t, dec_state, enc_hiddens, enc_hiddens_proj, enc_masks)

            # Append o_t to the combined_outputs
            # We memorize the combined_outputs in a list
            combined_outputs.append(o_t)

            # Update o_prev to the next o_t
            o_prev = o_t
        
        # Convert combined_outputs to a single tensor shape
        combined_outputs = torch.stack(combined_outputs, dim = 0)

        return combined_outputs
    
    def step(self, Ybar_t: torch.Tensor,
            dec_state: Tuple[torch.Tensor, torch.Tensor],
            enc_hiddens: torch.Tensor,
            enc_hiddens_proj: torch.Tensor,
            enc_masks: torch.Tensor) -> Tuple[Tuple, torch.Tensor, torch.Tensor]:
        """ Compute one forward step of the LSTM decoder, including the attention computation.

        @param Ybar_t (Tensor): Concatenated Tensor of [Y_t o_prev], with shape (b, e + h). The input for the decoder,
                                where b = batch size, e = embedding size, h = hidden size.
        @param dec_state (tuple(Tensor, Tensor)): Tuple of tensors both with shape (b, h), where b = batch size, h = hidden size.
                First tensor is decoder's prev hidden state, second tensor is decoder's prev cell.
        @param enc_hiddens (Tensor): Encoder hidden states Tensor, with shape (b, src_len, h * 2), where b = batch size,
                                    src_len = maximum source length, h = hidden size.
        @param enc_hiddens_proj (Tensor): Encoder hidden states Tensor, projected from (h * 2) to h. Tensor is with shape (b, src_len, h),
                                    where b = batch size, src_len = maximum source length, h = hidden size.
        @param enc_masks (Tensor): Tensor of sentence masks shape (b, src_len),
                                    where b = batch size, src_len is maximum source length. 

        @returns dec_state (tuple (Tensor, Tensor)): Tuple of tensors both shape (b, h), where b = batch size, h = hidden size.
                First tensor is decoder's new hidden state, second tensor is decoder's new cell.
        @returns combined_output (Tensor): Combined output Tensor at timestep t, shape (b, h), where b = batch size, h = hidden size.
        @returns e_t (Tensor): Tensor of shape (b, src_len). It is attention scores distribution.
                                Note: You will not use this outside of this function.
                                      We are simply returning this value so that we can sanity check
                                      your implementation.
        """

        combined_output = None

        # Apply the decoder to `Ybar_t` and `decocer hidden states` to obtain the new decoder hidden states
        # `Ybar_t` is the new input vector at decode for the timestep t
        # split dec_state into its two parts (dec_hidden, dec_cell)
        dec_state = self.decoder(Ybar_t, dec_state)
        dec_hidden, dec_cell = dec_state

        # compute the attention score e_t
        e_t = torch.squeeze(torch.bmm(enc_hiddens_proj, torch.unsqueeze(dec_hidden, dim = 2)), dim =2)

        # Set e_t to -inf where enc_masks has 1
        if enc_masks is not None:
            e_t.data.masked_fill_(enc_masks.bool(), -float('inf'))

        # Apply softmax to e_t to get alpha_t
        alpha_t = F.softmax(e_t, dim = 1)

        # obtain the attention output vector, a_t
        a_t = torch.squeeze(torch.bmm(torch.unsqueeze(alpha_t, dim = 1), enc_hiddens), dim = 1)

        # Concatenate the dec_hidden with a_t to compute U_t
        U_t = torch.cat((dec_hidden, a_t), dim = 1)

        # Apply the combined output projection to compute V_t
        V_t = self.combined_output_projection(U_t)

        # Compute the tesnor O_t by first applying Tanh and dropout
        O_t = self.dropout(torch.tanh(V_t))

        combined_output = O_t
        return dec_state, combined_output, e_t

        
    def generate_sent_masks(self, enc_hiddens: torch.Tensor, source_lengths: List[int]) -> torch.Tensor:
        """ Generate sentence masks for encoder hidden states.

        @param enc_hiddens (Tensor): encodings of shape (b, src_len, 2*h), where b = batch size,
                                     src_len = max source length, h = hidden size. 
        @param source_lengths (List[int]): List of actual lengths for each of the sentences in the batch.
        
        @returns enc_masks (Tensor): Tensor of sentence masks of shape (b, src_len),
                                    where src_len = max source length, h = hidden size.
        """

        enc_masks = torch.zeros(enc_hiddens.size(0), enc_hiddens.size(1), dtype = torch.float)
        for e_id, src_len in enumerate(source_lengths):
            enc_masks[e_id, src_len:] = 1
        return enc_masks.to(self.device)


    def beam_search(self, src_sent: List[str], beam_size: int=5, max_decoding_time_step: int=70) -> List[Hypothesis]:
        """ Given a single source sentence, perform beam search, yielding translations in the target language.
        @param src_sent (List[str]): a single source sentence (words)
        @param beam_size (int): beam size
        @param max_decoding_time_step (int): maximum number of time steps to unroll the decoding RNN
        @returns hypotheses (List[Hypothesis]): a list of hypothesis, each hypothesis has two fields:
                value: List[str]: the decoded target sentence, represented as a list of words
                score: float: the log-likelihood of the target sentence
        """
        src_sents_var = self.vocab.src.to_input_tensor([src_sent], self.device)

        src_encodings, dec_init_vec = self.encode(src_sents_var, [len(src_sent)])
        src_encodings_att_linear = self.att_projection(src_encodings)

        h_tm1 = dec_init_vec
        att_tm1 = torch.zeros(1, self.hidden_size, device=self.device)

        eos_id = self.vocab.tgt['</s>']

        hypotheses = [['<s>']]
        hyp_scores = torch.zeros(len(hypotheses), dtype=torch.float, device=self.device)
        completed_hypotheses = []

        t = 0
        while len(completed_hypotheses) < beam_size and t < max_decoding_time_step:
            t += 1
            hyp_num = len(hypotheses)

            exp_src_encodings = src_encodings.expand(hyp_num,
                                                     src_encodings.size(1),
                                                     src_encodings.size(2))

            exp_src_encodings_att_linear = src_encodings_att_linear.expand(hyp_num,
                                                                           src_encodings_att_linear.size(1),
                                                                           src_encodings_att_linear.size(2))

            y_tm1 = torch.tensor([self.vocab.tgt[hyp[-1]] for hyp in hypotheses], dtype=torch.long, device=self.device)
            y_t_embed = self.model_embeddings.target(y_tm1)

            x = torch.cat([y_t_embed, att_tm1], dim=-1)

            (h_t, cell_t), att_t, _  = self.step(x, h_tm1,
                                                      exp_src_encodings, exp_src_encodings_att_linear, enc_masks=None)

            # log probabilities over target words
            log_p_t = F.log_softmax(self.target_vocab_projection(att_t), dim=-1)

            live_hyp_num = beam_size - len(completed_hypotheses)
            contiuating_hyp_scores = (hyp_scores.unsqueeze(1).expand_as(log_p_t) + log_p_t).view(-1)
            top_cand_hyp_scores, top_cand_hyp_pos = torch.topk(contiuating_hyp_scores, k=live_hyp_num)

            prev_hyp_ids = top_cand_hyp_pos / len(self.vocab.tgt)
            hyp_word_ids = top_cand_hyp_pos % len(self.vocab.tgt)

            new_hypotheses = []
            live_hyp_ids = []
            new_hyp_scores = []

            for prev_hyp_id, hyp_word_id, cand_new_hyp_score in zip(prev_hyp_ids, hyp_word_ids, top_cand_hyp_scores):
                prev_hyp_id = prev_hyp_id.item()
                hyp_word_id = hyp_word_id.item()
                cand_new_hyp_score = cand_new_hyp_score.item()

                hyp_word = self.vocab.tgt.id2word[hyp_word_id]
                new_hyp_sent = hypotheses[prev_hyp_id] + [hyp_word]
                if hyp_word == '</s>':
                    completed_hypotheses.append(Hypothesis(value=new_hyp_sent[1:-1],
                                                           score=cand_new_hyp_score))
                else:
                    new_hypotheses.append(new_hyp_sent)
                    live_hyp_ids.append(prev_hyp_id)
                    new_hyp_scores.append(cand_new_hyp_score)

            if len(completed_hypotheses) == beam_size:
                break

            live_hyp_ids = torch.tensor(live_hyp_ids, dtype=torch.long, device=self.device)
            h_tm1 = (h_t[live_hyp_ids], cell_t[live_hyp_ids])
            att_tm1 = att_t[live_hyp_ids]

            hypotheses = new_hypotheses
            hyp_scores = torch.tensor(new_hyp_scores, dtype=torch.float, device=self.device)

        if len(completed_hypotheses) == 0:
            completed_hypotheses.append(Hypothesis(value=hypotheses[0][1:],
                                                   score=hyp_scores[0].item()))

        completed_hypotheses.sort(key=lambda hyp: hyp.score, reverse=True)

        return completed_hypotheses

    @property
    def device(self) -> torch.device:
        """ Determine which device to place the Tensors upon, CPU or GPU.
        """
        return self.model_embeddings.source.weight.device

    @staticmethod
    def load(model_path: str):
        """ Load the model from a file.
        @param model_path (str): path to model
        """
        params = torch.load(model_path, map_location=lambda storage, loc: storage)
        args = params['args']
        model = NMT(vocab=params['vocab'], **args)
        model.load_state_dict(params['state_dict'])

        return model

    def save(self, path: str):
        """ Save the odel to a file.
        @param path (str): path to the model
        """
        print('save model parameters to [%s]' % path, file=sys.stderr)

        params = {
            'args': dict(embed_size=self.model_embeddings.embed_size, hidden_size=self.hidden_size, dropout_rate=self.dropout_rate),
            'vocab': self.vocab,
            'state_dict': self.state_dict()
        }

        torch.save(params, path)