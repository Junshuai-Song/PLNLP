# -*- coding: utf-8 -*-
from torch.utils.data import DataLoader
from plnlp.layer import *
from plnlp.loss import *
from plnlp.utils import *


class BaseModel(object):
    """
        Parameters
        ----------
        lr : double
            Learning rate
        dropout : double
            dropout probability for gnn and mlp layers
        gnn_num_layers : int
            number of gnn layers
        mlp_num_layers : int
            number of gnn layers
        *_hidden_channels : int
            dimension of hidden
        num_nodes : int
            number of graph nodes
        num_node_feats : int
            dimension of raw node features
        gnn_encoder_name : str
            gnn encoder name
        predictor_name: str
            link predictor name
        activation_name: str
            activation function name
        loss_func: str
            loss function name
        optimizer_name: str
            optimization method name
        device: str
            device name: gpu or cpu
        use_node_feats: bool
            whether to use raw node features as input
        train_node_emb:
            whether to train node embeddings based on node id
        pretrain_emb:
            whether to load pretrained node embeddings
        node_feat_trans:
            whether to do linear transformation for node features
    """

    def __init__(self, lr, dropout, gnn_num_layers, mlp_num_layers, emb_hidden_channels, gnn_hidden_channels,
                 mlp_hidden_channels, num_nodes, num_node_feats, gnn_encoder_name, predictor_name, activation_name,
                 loss_func, optimizer_name, device, use_node_feats, train_node_emb, pretrain_emb, node_feat_trans):
        self.loss_func_name = loss_func
        self.num_nodes = num_nodes
        self.num_node_feats = num_node_feats
        self.use_node_feats = use_node_feats
        self.train_node_emb = train_node_emb
        self.node_feat_trans = node_feat_trans
        self.device = device

        # Input Layer
        self.input_dim, self.emb, self.feat_trans_lin = create_input_layer(num_nodes=num_nodes,
                                                                           num_node_feats=num_node_feats,
                                                                           hidden_channels=emb_hidden_channels,
                                                                           use_node_feats=use_node_feats,
                                                                           train_node_emb=train_node_emb,
                                                                           pretrain_emb=pretrain_emb,
                                                                           node_feat_trans=node_feat_trans)
        if self.emb is not None:
            self.emb = self.emb.to(device)
        if self.feat_trans_lin is not None:
            self.feat_trans_lin = self.feat_trans_lin.to(device)

        # GNN Layer
        self.encoder = create_gnn_layer(input_dim=self.input_dim,
                                        hidden_channels=gnn_hidden_channels,
                                        num_layers=gnn_num_layers,
                                        dropout=dropout,
                                        activation=activation_name,
                                        encoder_name=gnn_encoder_name)
        self.encoder = self.encoder.to(device)

        # Predict Layer
        self.predictor = create_predictor_layer(hidden_channels=mlp_hidden_channels,
                                                num_layers=mlp_num_layers,
                                                dropout=dropout,
                                                activation=activation_name,
                                                predictor_name=predictor_name)
        self.predictor = self.predictor.to(device)

        # Parameters and Optimizer
        para_list = list(self.encoder.parameters()) + list(self.predictor.parameters())
        if self.emb is not None:
            para_list += list(self.emb.parameters())
        if self.feat_trans_lin is not None:
            para_list += list(self.feat_trans_lin.parameters())
        if optimizer_name == 'AdamW':
            self.optimizer = torch.optim.AdamW(para_list, lr=lr)
        else:
            self.optimizer = torch.optim.Adam(para_list, lr=lr)

    def param_init(self):
        self.encoder.reset_parameters()
        self.predictor.reset_parameters()
        if self.emb is not None:
            torch.nn.init.xavier_uniform_(self.emb.weight)
        if self.feat_trans_lin is not None:
            self.feat_trans_lin.reset_parameters()

    def create_input_feat(self, data):
        if self.use_node_feats:
            if self.node_feat_trans:
                input_feat = self.feat_trans_lin(data.x.to(self.device))
            else:
                input_feat = data.x.to(self.device)
            if self.train_node_emb:
                input_feat = torch.cat([self.emb.weight, input_feat], dim=-1)
        else:
            input_feat = self.emb.weight
        return input_feat

    def calculate_loss(self, pos_out, neg_out, num_neg, margin=None):
        if self.loss_func_name == 'CE':
            loss = ce_loss(pos_out, neg_out)
        elif self.loss_func_name == 'InfoNCE':
            loss = info_nce_loss(pos_out, neg_out, num_neg)
        elif self.loss_func_name == 'LogRank':
            loss = log_rank_loss(pos_out, neg_out, num_neg)
        elif self.loss_func_name == 'AdaAUC' and margin is not None:
            loss = adaptive_auc_loss(pos_out, neg_out, num_neg, margin)
        else:
            loss = auc_loss(pos_out, neg_out, num_neg)
        return loss

    def train(self, data, split_edge, batch_size, neg_sampler_name, num_neg):
        self.encoder.train()
        self.predictor.train()

        pos_train_edge, neg_train_edge = get_pos_neg_edges('train', split_edge,
                                                           edge_index=data.edge_index,
                                                           num_nodes=self.num_nodes,
                                                           neg_sampler_name=neg_sampler_name,
                                                           num_neg=num_neg)

        pos_train_edge, neg_train_edge = pos_train_edge.to(
            self.device), neg_train_edge.to(self.device)

        if 'weight' in split_edge['train']:
            edge_weight_margin = split_edge['train']['weight'].to(self.device)
        else:
            edge_weight_margin = None

        total_loss = total_examples = 0
        for perm in DataLoader(range(pos_train_edge.size(0)), batch_size,
                               shuffle=True):
            self.optimizer.zero_grad()

            input_feat = self.create_input_feat(data)
            h = self.encoder(input_feat, data.adj_t)
            pos_edge = pos_train_edge[perm].t()
            neg_edge = torch.reshape(neg_train_edge[perm], (-1, 2)).t()

            pos_out = self.predictor(h[pos_edge[0]], h[pos_edge[1]])
            neg_out = self.predictor(h[neg_edge[0]], h[neg_edge[1]])

            weight_margin = edge_weight_margin[perm] if edge_weight_margin is not None else None

            loss = self.calculate_loss(pos_out, neg_out, num_neg, margin=weight_margin)
            loss.backward()

            # torch.nn.utils.clip_grad_norm_(self.encoder.parameters(), 1.0)
            # torch.nn.utils.clip_grad_norm_(self.predictor.parameters(), 1.0)
            self.optimizer.step()

            num_examples = pos_out.size(0)
            total_loss += loss.item() * num_examples
            total_examples += num_examples

        return total_loss / total_examples

    @torch.no_grad()
    def batch_predict(self, h, edges, batch_size):
        preds = []
        for perm in DataLoader(range(edges.size(0)), batch_size):
            edge = edges[perm].t()
            preds += [self.predictor(h[edge[0]], h[edge[1]]).squeeze().cpu()]
        pred = torch.cat(preds, dim=0)
        return pred

    @torch.no_grad()
    def test(self, data, split_edge, batch_size, evaluator, eval_metric):
        self.encoder.eval()
        self.predictor.eval()

        input_feat = self.create_input_feat(data)
        h = self.encoder(input_feat, data.adj_t)
        # The default index of unseen nodes is -1,
        # hidden representations of unseen nodes is the average of all seen node representations
        mean_h = torch.mean(h, dim=0, keepdim=True)
        h = torch.cat([h, mean_h], dim=0)

        pos_valid_edge, neg_valid_edge = get_pos_neg_edges('valid', split_edge)
        pos_test_edge, neg_test_edge = get_pos_neg_edges('test', split_edge)
        pos_valid_edge, neg_valid_edge = pos_valid_edge.to(self.device), neg_valid_edge.to(self.device)
        pos_test_edge, neg_test_edge = pos_test_edge.to(self.device), neg_test_edge.to(self.device)

        pos_valid_pred = self.batch_predict(h, pos_valid_edge, batch_size)
        neg_valid_pred = self.batch_predict(h, neg_valid_edge, batch_size)

        h = self.encoder(input_feat, data.adj_t)
        mean_h = torch.mean(h, dim=0, keepdim=True)
        h = torch.cat([h, mean_h], dim=0)

        pos_test_pred = self.batch_predict(h, pos_test_edge, batch_size)
        neg_test_pred = self.batch_predict(h, neg_test_edge, batch_size)

        if eval_metric == 'hits':
            results = evaluate_hits(
                evaluator,
                pos_valid_pred,
                neg_valid_pred,
                pos_test_pred,
                neg_test_pred)
        else:
            results = evaluate_mrr(
                evaluator,
                pos_valid_pred,
                neg_valid_pred,
                pos_test_pred,
                neg_test_pred)

        return results


def create_input_layer(num_nodes, num_node_feats, hidden_channels,
                       use_node_feats=True, train_node_emb=False, pretrain_emb=None, node_feat_trans=False):
    emb = None
    feat_trans_lin = None
    if use_node_feats:
        if node_feat_trans:
            feat_trans_lin = torch.nn.Linear(
                num_node_feats, hidden_channels, bias=False)
            input_dim = hidden_channels
        else:
            input_dim = num_node_feats
        if train_node_emb:
            emb = torch.nn.Embedding(num_nodes, hidden_channels)
            input_dim += hidden_channels
        elif pretrain_emb is not None and pretrain_emb != '':
            weight = torch.load(pretrain_emb)
            emb = torch.nn.Embedding.from_pretrained(weight)
            emb.weight.requires_grad = False
            input_dim += emb.weight.size(1)
    else:
        if pretrain_emb is not None and pretrain_emb != '':
            weight = torch.load(pretrain_emb)
            emb = torch.nn.Embedding.from_pretrained(weight)
            emb.weight.requires_grad = False
            input_dim = emb.weight.size(1)
        else:
            emb = torch.nn.Embedding(num_nodes, hidden_channels)
            input_dim = hidden_channels
    return input_dim, emb, feat_trans_lin


def create_gnn_layer(input_dim, hidden_channels, num_layers, dropout, activation='relu', encoder_name='SAGE'):
    if encoder_name.upper() == 'GCN':
        return GCN(input_dim, hidden_channels, hidden_channels, num_layers, dropout, activation)
    elif encoder_name.upper() == 'WSAGE':
        return WSAGE(input_dim, hidden_channels, hidden_channels, num_layers, dropout, activation)
    elif encoder_name.upper() == 'TRANSFORMER':
        return Transformer(input_dim, hidden_channels, hidden_channels, num_layers, dropout, activation)
    else:
        return SAGE(input_dim, hidden_channels, hidden_channels, num_layers, dropout, activation)


def create_predictor_layer(hidden_channels=256, num_layers=2, dropout=0, activation='relu', predictor_name='MLP'):
    predictor_name = predictor_name.upper()
    if predictor_name == 'DOT':
        return DotPredictor()
    elif predictor_name == 'BIL':
        return BilinearPredictor(hidden_channels)
    elif predictor_name == 'MLP':
        return MLPPredictor(hidden_channels, hidden_channels, 1, num_layers, dropout, activation)
    elif predictor_name == 'MLPDOT':
        return MLPDotPredictor(hidden_channels, 1, num_layers, dropout, activation)
    elif predictor_name == 'MLPBIL':
        return MLPBilPredictor(hidden_channels, 1, num_layers, dropout, activation)
    elif predictor_name == 'MLPCAT':
        return MLPCatPredictor(hidden_channels, hidden_channels, 1, num_layers, dropout, activation)