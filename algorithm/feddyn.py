"""
@inproceedings{
    acar2021federated,
    title={Federated Learning Based on Dynamic Regularization},
    author={Durmus Alp Emre Acar and Yue Zhao and Ramon Matas and Matthew Mattina and Paul Whatmough and Venkatesh Saligrama},
    booktitle={International Conference on Learning Representations},
    year={2021},
    url={https://openreview.net/forum?id=B7v4QMR6Z9w}
}
"""

import copy

import torch
from torch.utils import data

from algorithm.algorithm_base import Algorithm
from client import Client
from dataset import Dataset
from server import Server
from utils import *


class FedDyn(Algorithm):
    def __init__(
        self,
        lr,
        lr_decay_per_round,
        batch_size,
        epoch,
        weight_decay,
        model_func,
        n_param,
        max_norm,
        noiseless,
        dataset_name,
        save_period,
        print_per,
        alpha_coef,
    ):
        super().__init__(
            "FedDyn",
            lr,
            lr_decay_per_round,
            batch_size,
            epoch,
            weight_decay,
            model_func,
            n_param,
            max_norm,
            noiseless,
            dataset_name,
            save_period,
            print_per,
        )

        self.alpha_coef = alpha_coef

    # override
    def local_train(self, client: Client, inputs: dict):
        self.device = client.device

        client.model = self.model_func().to(self.device)
        model = client.model
        # Warm start from current avg model
        model.load_state_dict(
            copy.deepcopy(dict(inputs["cloud_model"].named_parameters()))
        )
        for params in model.parameters():
            params.requires_grad = True

        # Scale down
        alpha_coef_adpt = self.alpha_coef / client.weight  # adaptive alpha coef
        local_param_list_curr = torch.tensor(
            inputs["local_param"], dtype=torch.float32, device=self.device
        )  # = local_grad_vector
        print("local_param_list_curr = ", local_param_list_curr)
        print("cloud_model_param_tensor = ", inputs["cloud_model_param_tensor"])
        client.model = self.__train_model(
            model,
            alpha_coef_adpt,
            inputs["cloud_model_param_tensor"],
            local_param_list_curr,
            client.train_data_X,
            client.train_data_Y,
            inputs["curr_round"],
        )
        curr_model_par = get_model_params([client.model], self.n_param)[
            0
        ]  # get the model parameter after running FedDyn
        print("curr_model_par = ", curr_model_par)

        # No need to scale up hist terms. They are -\nabla/alpha and alpha is already scaled.
        inputs["local_param"] += (
            curr_model_par - inputs["cloud_model_param"]
        )  # after training, dynamically update the weight with the cloud model parameters

        client.client_param = curr_model_par

    def __train_model(
        self,
        model,
        alpha_coef_adpt,
        avg_mdl_param,
        local_grad_vector,
        trn_x,
        trn_y,
        curr_round,
    ):
        decayed_lr = self.lr * (self.lr_decay_per_round**curr_round)

        n_trn = trn_x.shape[0]
        trn_gen = data.DataLoader(
            Dataset(trn_x, trn_y, train=True, dataset_name=self.dataset_name),
            batch_size=self.batch_size,
            shuffle=True,
        )
        loss_fn = torch.nn.CrossEntropyLoss(reduction="sum")

        optimizer = torch.optim.SGD(
            model.parameters(),
            lr=decayed_lr,
            weight_decay=alpha_coef_adpt + self.weight_decay,
        )
        model.train()
        model = model.to(self.device)

        for e in range(self.epoch):
            # Training
            epoch_loss = 0
            trn_gen_iter = trn_gen.__iter__()
            for _ in range(int(np.ceil(n_trn / self.batch_size))):
                batch_x, batch_y = trn_gen_iter.__next__()
                batch_x = batch_x.to(self.device)
                batch_y = batch_y.to(self.device)

                y_pred = model(batch_x)

                ## Get f_i estimate
                loss_f_i = loss_fn(y_pred, batch_y.reshape(-1).long())
                loss_f_i = loss_f_i / list(batch_y.size())[0]

                # Get linear penalty on the current parameter estimates
                local_par_list = None
                for param in model.parameters():
                    if not isinstance(local_par_list, torch.Tensor):
                        # Initially nothing to concatenate
                        local_par_list = param.reshape(-1)
                    else:
                        local_par_list = torch.cat(
                            (local_par_list, param.reshape(-1)), 0
                        )

                loss_algo = alpha_coef_adpt * torch.sum(
                    local_par_list * (-avg_mdl_param + local_grad_vector)
                )
                loss = loss_f_i + loss_algo

                optimizer.zero_grad()
                loss.backward()
                torch.nn.utils.clip_grad_norm_(
                    parameters=model.parameters(), max_norm=self.max_norm
                )  # Clip gradients
                optimizer.step()
                epoch_loss += loss.item() * list(batch_y.size())[0]

            if (e + 1) % self.print_per == 0:
                epoch_loss /= n_trn
                if self.weight_decay != None:
                    # Add L2 loss to complete f_i
                    params = get_model_params([model], self.n_param)
                    epoch_loss += (
                        (alpha_coef_adpt + self.weight_decay)
                        / 2
                        * np.sum(params * params)
                    )
                print("Epoch %3d, Training Loss: %.4f" % (e + 1, epoch_loss))
                model.train()

        # Freeze model
        for params in model.parameters():
            params.requires_grad = False
        model.eval()

        return model

    # override
    def aggregate(self, server: Server, inputs: dict):
        clients_list = inputs["clients_list"]
        selected_clnts_idx = inputs["selected_clnts_idx"]

        clients_param_list = np.array([client.client_param for client in clients_list])

        avg_mdl_param = (
            inputs["avg_mdl_param"]
            if not self.noiseless
            else np.mean(clients_param_list[selected_clnts_idx], axis=0)
        )

        print("avg_mdl_param = ", avg_mdl_param)
        # print("n_param = ", self.n_param)
        # print("avg_mdl_param.shape = ", avg_mdl_param.shape)

        inputs["cloud_model_param"] = avg_mdl_param + np.mean(
            inputs["local_param_list"], axis=0
        )

        server.avg_model = set_model(self.model_func(), avg_mdl_param, server.device)
        server.all_model = set_model(
            self.model_func(), np.mean(clients_param_list, axis=0), server.device
        )
        inputs["cloud_model"] = set_model(
            self.model_func().to(server.device),
            inputs["cloud_model_param"],
            server.device,
        )
