import numpy as np
import torch
from torch.autograd import Variable
from torch.optim import Adam
MSELoss = torch.nn.MSELoss()


from utils.networks import MLPNetwork
from utils.misc import gumbel_softmax, onehot_from_logits


class VariationalBaseEmpowerment(object):
    def __init__(self):
        pass

    def compute(self):
        raise NotImplementedError

    def update(self):
        raise NotImplementedError

    def prep_training(self):
        raise NotImplementedError

    def prep_rollouts(self):
        raise NotImplementedError


class VariationalJointEmpowerment(VariationalBaseEmpowerment):
    def __init__(self, init_params, lr=0.01):
        super(VariationalJointEmpowerment, self).__init__()
        self.transition = MLPNetwork(init_params['num_in_trans'], init_params['num_out_trans'], recurrent=True)
        self.source = MLPNetwork(init_params['num_in_src'], init_params['num_out_src'], recurrent=True)
        self.planning = MLPNetwork(init_params['num_in_plan'], init_params['num_out_plan'], recurrent=True)

        self.transition_optimizer = Adam(self.transition.parameters(), lr=lr)
        self.planning_optimizer = Adam(self.planning.parameters(), lr=lr)
        self.source_optimizer = Adam(list(self.source.parameters()) + list(self.planning.parameters()), lr=lr)
        self.trans_dev = 'cpu'  # device for transition
        self.source_dev = 'cpu'
        self.plan_dev = 'cpu'

        self.niter = 0

    def compute(self, rewards, next_obs):
        with torch.no_grad():
            next_obs = [Variable(torch.Tensor(np.vstack(next_obs[:, i])),
                      requires_grad=False) for i in range(rewards.shape[1])]

            acs_src = []
            prob_src = []
            for no in next_obs:
                acs_src.append(gumbel_softmax(self.source(no), device=self.source_dev, hard=True))
                prob_src.append(gumbel_softmax(self.source(no), device=self.source_dev, hard=False))

            trans_in = torch.cat((*next_obs, *acs_src), dim=1)
            trans_out = self.transition(trans_in)
            prob_plan = []
            n_obs = len(next_obs[0][0])
            for i, no in enumerate(next_obs):
                nno = trans_out[:, i * n_obs:(i + 1) * n_obs]
                plan_in = torch.cat((no, nno), dim=1)
                prob_plan.append(gumbel_softmax(self.planning(plan_in), device=self.plan_dev, hard=False))
            prob_plan = torch.cat(prob_plan, dim=1)
            prob_src = torch.cat(prob_src, dim=1)
            acs_src = torch.cat(acs_src, dim=1)

            E = acs_src * prob_plan - acs_src * prob_src
            i_rews = E.mean() * torch.ones((1, rewards.shape[1]))
            return i_rews.numpy()

    def update(self, sample, logger=None):
        obs, acs, rews, emps, next_obs, dones = sample

        self.transition_optimizer.zero_grad()
        trans_in = torch.cat((*obs, *acs), dim=1)
        next_obs_pred = self.transition(trans_in)
        trans_loss = MSELoss(next_obs_pred, torch.cat(next_obs, dim=1))
        trans_loss.backward()
        self.transition_optimizer.step()

        self.planning_optimizer.zero_grad()
        acs_plan = []
        for o, no in zip(obs, next_obs):
            plan_in = torch.cat((o, no), dim=1)
            acs_plan.append(gumbel_softmax(self.planning(plan_in), device=self.plan_dev, hard=True))
        acs_plan = torch.cat(acs_plan, dim=1)
        acs_torch = torch.cat(acs, dim=1)
        plan_loss = MSELoss(acs_plan, acs_torch)
        plan_loss.backward()
        self.planning_optimizer.step()

        self.source_optimizer.zero_grad()
        acs_src = []
        prob_src = []
        for no in next_obs:
            acs_src.append(gumbel_softmax(self.source(no), device=self.source_dev, hard=True))
            prob_src.append(gumbel_softmax(self.source(no), device=self.source_dev, hard=False))
        with torch.no_grad():
            trans_in = torch.cat((*next_obs, *acs_src), dim=1)
            trans_out = self.transition(trans_in)
        prob_plan = []
        n_obs = len(next_obs[0][0])
        for i, no in enumerate(next_obs):
            nno = trans_out[:, i*n_obs:(i+1)*n_obs]
            plan_in = torch.cat((no, nno), dim=1)
            prob_plan.append(gumbel_softmax(self.planning(plan_in), device=self.plan_dev, hard=False))
        prob_plan = torch.cat(prob_plan, dim=1)
        prob_src = torch.cat(prob_src, dim=1)
        acs_src = torch.cat(acs_src, dim=1)

        E = acs_src * prob_plan - acs_src * prob_src
        i_rews = -E.mean()
        i_rews.backward()
        self.source_optimizer.step()

        if logger is not None:
            logger.add_scalars('empowerment/losses',
                               {'trans_loss': trans_loss.detach(),
                                'plan_loss': plan_loss.detach(),
                                'i_rews': i_rews.detach()},
                               self.niter)
        self.niter += 1

    def prep_training(self, device='gpu'):
        self.transition.train()
        self.source.train()
        self.planning.train()

        if device == 'gpu':
            fn = lambda x: x.cuda()
        else:
            fn = lambda x: x.cpu()
        if not self.trans_dev == device:
            self.transition = fn(self.transition)
            self.trans_dev = device
        if not self.source_dev == device:
            self.source = fn(self.source)
            self.source_dev = device
        if not self.plan_dev == device:
            self.planning = fn(self.planning)
            self.plan_dev = device

    def prep_rollouts(self, device='cpu'):
        self.transition.eval()
        self.source.eval()
        self.planning.eval()
        if device == 'gpu':
            fn = lambda x: x.cuda()
        else:
            fn = lambda x: x.cpu()
        # only need main policy for rollouts
        if not self.trans_dev == device:
            self.transition = fn(self.transition)
            self.trans_dev = device
        if not self.source_dev == device:
            self.source = fn(self.source)
            self.source_dev = device
        if not self.plan_dev == device:
            self.planning = fn(self.planning)
            self.plan_dev = device

    @classmethod
    def init_from_env(cls, env):
        num_in_source = num_out_source = num_in_planning = \
            num_out_planning = num_in_transition = num_out_transition = 0
        for acsp, obsp in zip(env.action_space, env.observation_space):

            num_in_source = obsp.shape[0]
            num_out_source = acsp.n

            num_in_planning = 2*obsp.shape[0]
            num_out_planning = acsp.n

            num_in_transition += obsp.shape[0] + acsp.n
            num_out_transition += obsp.shape[0]

        init_params = {'num_in_src': num_in_source,
                        'num_in_plan': num_in_planning,
                        'num_in_trans': num_in_transition,
                        'num_out_src': num_out_source,
                        'num_out_plan': num_out_planning,
                        'num_out_trans': num_out_transition}

        instance = cls(init_params)
        instance.init_dict = init_params
        return instance


class VariationalTransferEmpowerment(VariationalBaseEmpowerment):
    def __init__(self, init_params, num_in_trans, num_out_trans, lr=0.01, hidden_dim=64, recurrent=False,
                 convolutional=False):
        super(VariationalTransferEmpowerment, self).__init__()
        self.transition = MLPNetwork(num_in_trans, num_out_trans, recurrent=True)
        self.source = [MLPNetwork(p['num_in_src'], p['num_out_src'], recurrent=True) for p in init_params]
        self.planning = [MLPNetwork(p['num_in_plan'], p['num_out_plan'], recurrent=True) for p in init_params]

        self.transition_optimizer = Adam(self.transition.parameters(), lr=lr)
        params_planning = []
        for mlp in self.planning: params_planning += list(mlp.parameters())
        self.planning_optimizer = Adam(params_planning, lr=lr)
        params_source = []
        for mlp in self.source: params_source += list(mlp.parameters())
        self.source_optimizer = Adam(params_source, lr=lr)
        self.trans_dev = 'cpu'  # device for transition
        self.source_dev = 'cpu'
        self.plan_dev = 'cpu'

        self.niter = 0

    def compute(self, rewards, next_obs):
        with torch.no_grad():
            next_obs = [Variable(torch.Tensor(np.vstack(next_obs[:, i])),
                      requires_grad=False) for i in range(rewards.shape[1])]

            acs_src = []
            prob_src = []
            for no, source in zip(next_obs, self.source):
                acs_src.append(gumbel_softmax(source(no), device=self.source_dev, hard=True))
                prob_src.append(gumbel_softmax(source(no), device=self.source_dev, hard=False))

            trans_in = torch.cat((*next_obs, *acs_src), dim=1)
            trans_out = self.transition(trans_in)
            prob_plan = []
            start = 0
            for i, (no, planning) in enumerate(zip(next_obs, self.planning)):
                length = no.shape[1]
                nno = trans_out[:, start:start + length]
                plan_in = torch.cat((no, nno), dim=1)
                prob_plan.append(gumbel_softmax(planning(plan_in), device=self.plan_dev, hard=False))
            prob_plan = torch.cat(prob_plan, dim=1)
            prob_src = torch.cat(prob_src, dim=1)
            acs_src = torch.cat(acs_src, dim=1)

            E = acs_src * prob_plan - acs_src * prob_src
            i_rews = E.mean() * torch.ones((1, rewards.shape[1]))
            return i_rews.numpy()

    def update(self, sample, logger=None):
        obs, acs, rews, emps, next_obs, dones = sample

        self.transition_optimizer.zero_grad()
        trans_in = torch.cat((*obs, *acs), dim=1)
        next_obs_pred = self.transition(trans_in)
        trans_loss = MSELoss(next_obs_pred, torch.cat(next_obs, dim=1))
        trans_loss.backward()
        self.transition_optimizer.step()

        self.planning_optimizer.zero_grad()
        acs_plan = []
        for o, no, planning in zip(obs, next_obs, self.planning):
            plan_in = torch.cat((o, no), dim=1)
            acs_plan.append(gumbel_softmax(planning(plan_in), device=self.plan_dev, hard=True))
        acs_plan = torch.cat(acs_plan, dim=1)
        acs_torch = torch.cat(acs, dim=1)
        plan_loss = MSELoss(acs_plan, acs_torch)
        plan_loss.backward()
        self.planning_optimizer.step()

        self.source_optimizer.zero_grad()
        acs_src = []
        prob_src = []
        for no, source in zip(next_obs, self.source):
            acs_src.append(gumbel_softmax(source(no), device=self.source_dev, hard=True))
            prob_src.append(gumbel_softmax(source(no), device=self.source_dev, hard=False))
        with torch.no_grad():
            trans_in = torch.cat((*next_obs, *acs_src), dim=1)
            trans_out = self.transition(trans_in)
        prob_plan = []
        start = 0
        for i, (no, planning) in enumerate(zip(next_obs, self.planning)):
            length = no.shape[1]
            nno = trans_out[:, start:start+length]
            plan_in = torch.cat((no, nno), dim=1)
            prob_plan.append(gumbel_softmax(planning(plan_in), device=self.plan_dev, hard=False))
            start += length
        prob_plan = torch.cat(prob_plan, dim=1)
        prob_src = torch.cat(prob_src, dim=1)
        acs_src = torch.cat(acs_src, dim=1)

        E = acs_src * prob_plan - acs_src * prob_src
        i_rews = -E.mean()
        i_rews.backward()
        self.source_optimizer.step()

        if logger is not None:
            logger.add_scalars('empowerment/losses',
                               {'trans_loss': trans_loss.detach(),
                                'plan_loss': plan_loss.detach(),
                                'i_rews': i_rews.detach()},
                               self.niter)
        self.niter += 1

    def prep_training(self, device='gpu'):
        self.transition.train()
        if device == 'gpu':
            fn = lambda x: x.cuda()
        else:
            fn = lambda x: x.cpu()
        if not self.trans_dev == device:
            self.transition = fn(self.transition)
            self.trans_dev = device
        if not self.source_dev == device:
            for source in self.source:
                source = fn(source)
                source.train()
            self.source_dev = device
        if not self.plan_dev == device:
            for planning in self.planning:
                planning = fn(planning)
                planning.train()
            self.plan_dev = device

    def prep_rollouts(self, device='cpu'):
        self.transition.eval()
        if device == 'gpu':
            fn = lambda x: x.cuda()
        else:
            fn = lambda x: x.cpu()
        # only need main policy for rollouts
        if not self.trans_dev == device:
            self.transition = fn(self.transition)
            self.trans_dev = device
        self.transition.eval()
        if not self.source_dev == device:
            for source in self.source:
                source = fn(source)
            self.source_dev = device
        for source in self.source:
            source.eval()
        if not self.plan_dev == device:
            for planning in self.planning:
                planning = fn(planning)
            self.plan_dev = device
        for planning in self.planning:
            planning.eval()


    @classmethod
    def init_from_env(cls, env, lr=0.01, hidden_dim=64, recurrent=False, convolutional=False):
        """
        Instantiate instance of this class from multi-agent environment
        """
        init_params = []

        num_in_transition = num_out_transition = 0
        for acsp, obsp in zip(env.action_space, env.observation_space):
            num_in_source = obsp.shape[0]
            num_out_source = acsp.n

            num_in_planning = 2 * obsp.shape[0]
            num_out_planning = acsp.n

            num_in_transition += obsp.shape[0] + acsp.n
            num_out_transition += obsp.shape[0]

            init_params.append({'num_in_src': num_in_source,
                    'num_in_plan': num_in_planning,
                    'num_out_src': num_out_source,
                    'num_out_plan': num_out_planning})

        init_dict = {'lr': lr,
                    'hidden_dim': hidden_dim,
                    'init_params': init_params,
                     'num_in_trans': num_in_transition,
                     'num_out_trans': num_out_transition,
                     'recurrent': recurrent,
                     'convolutional': convolutional}
        instance = cls(**init_dict)
        instance.init_dict = init_dict
        return instance