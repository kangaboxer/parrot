import os
from algorithms import Adasecant
from blocks import initialization
from blocks.algorithms import (
    Adam, CompositeRule, GradientDescent, StepClipping)
from blocks.extensions import (Printing, Timing)
from blocks.extensions.monitoring import (
    DataStreamMonitoring, TrainingDataMonitoring)
from blocks.extensions.predicates import OnLogRecord
from blocks.extensions.saveload import Checkpoint, Load
from blocks.extensions.training import TrackTheBest
from blocks.graph import ComputationGraph
from blocks.main_loop import MainLoop
from blocks.model import Model
import cPickle
from extensions import LearningRateSchedule, Plot, TimedFinish
from blizzard import phonemes_stream
from model import PhonemesParrot
from utils import train_phonemes_parse

parser = train_phonemes_parse()
args = parser.parse_args()

if args.algorithm == "adasecant":
    args.lr_schedule = False

exp_name = args.experiment_name
save_dir = args.save_dir

print "Saving config ..."
with open(os.path.join(save_dir, 'config', exp_name + '.pkl'), 'w') as f:
    cPickle.dump(args, f)
print "Finished saving."

w_init = initialization.IsotropicGaussian(0.01)
b_init = initialization.Constant(0.)

train_stream = phonemes_stream(('train',), args.batch_size)
valid_stream = phonemes_stream(('valid',), args.batch_size)

f0_tr, phonemes_tr, spectrum_tr, voiced_tr = \
    next(train_stream.get_epoch_iterator())

print "Shapes: "
print "f0_tr.shape", f0_tr.shape
print "phonemes_tr.shape", f0_tr.shape
print "spectrum_tr.shape", f0_tr.shape
print "voiced_tr.shape", voiced_tr.shape


parrot_args = {
    'num_freq': args.num_freq,
    'k': args.num_mixture,
    'rnn1_h_dim': args.rnn1_size,
    'num_phonemes': args.num_phonemes,
    'phonemes_embed_dim': args.phonemes_embed_dim,
    'sampling_bias': 0.,
    'weights_init': w_init,
    'biases_init': b_init,
    'name': 'parrot'}

parrot = PhonemesParrot(**parrot_args)
parrot.initialize()

f0, voiced, spectrum, phonemes = \
    parrot.symbolic_input_variables()

cost, extra_updates = parrot.compute_cost(
    f0, voiced, spectrum, phonemes, args.batch_size)

cost.name = 'nll'

cg = ComputationGraph(cost)
model = Model(cost)
parameters = cg.parameters

if args.algorithm == "adam":
    step_rule = CompositeRule(
        [StepClipping(10. * args.grad_clip), Adam(args.learning_rate)])
elif args.algorithm == "adasecant":
    step_rule = Adasecant(grad_clip=args.grad_clip)

algorithm = GradientDescent(
    cost=cost,
    parameters=parameters,
    step_rule=step_rule,
    on_unused_sources='warn')
algorithm.add_updates(extra_updates)

monitoring_vars = [cost]

if args.lr_schedule:
    lr = algorithm.step_rule.components[1].learning_rate
    monitoring_vars.append(lr)

train_monitor = TrainingDataMonitoring(
    variables=monitoring_vars,
    every_n_batches=args.save_every,
    prefix="train")

valid_monitor = DataStreamMonitoring(
    monitoring_vars,
    valid_stream,
    every_n_batches=args.save_every,
    prefix="valid")

# Multi GPU
worker = None
if args.platoon_port:
    from blocks_extras.extensions.synchronization import (
        Synchronize, SynchronizeWorker)
    from platoon.param_sync import ASGD

    sync_rule = ASGD()
    worker = SynchronizeWorker(
        sync_rule, control_port=args.platoon_port, socket_timeout=2000)

extensions = []

if args.load_experiment and (not worker or worker.is_main_worker):
    extensions += [Load(os.path.join(
        save_dir, "pkl", "best_" + args.load_experiment + ".tar"))]

extensions += [
    Timing(every_n_batches=args.save_every),
    train_monitor]

if not worker or worker.is_main_worker:
    extensions += [
        valid_monitor,
        TrackTheBest(
            'valid_nll',
            every_n_batches=args.save_every,
            before_first_epoch=True),
        Plot(
            os.path.join(save_dir, "progress", exp_name + ".png"),
            [['train_nll', 'valid_nll']],
            every_n_batches=args.save_every,
            email=False),
        Checkpoint(
            os.path.join(save_dir, "pkl", "best_" + exp_name + ".tar"),
            after_training=False,
            save_separately=['log'],
            use_cpickle=True,
            save_main_loop=False,
            before_first_epoch=True)
        .add_condition(
            ["after_batch", "before_training"],
            predicate=OnLogRecord('valid_nll_best_so_far')),
        Checkpoint(
            os.path.join(save_dir, "pkl", "last_" + exp_name + ".tar"),
            after_training=True,
            save_separately=['log'],
            use_cpickle=True,
            every_n_batches=args.save_every,
            save_main_loop=False)]

    if args.lr_schedule:
        extensions += [
            LearningRateSchedule(
                lr, 'valid_nll',
                os.path.join(save_dir, "pkl", "best_" + exp_name + ".tar"),
                patience=10,
                num_cuts=5,
                every_n_batches=args.save_every)]

if worker:
    extensions += [
        Synchronize(
            worker,
            after_batch=True,
            before_epoch=True)]

extensions += [
    Printing(
        after_epoch=False,
        every_n_batches=args.save_every)]

if args.time_limit:
    extensions += [TimedFinish(args.time_limit)]

main_loop = MainLoop(
    model=model,
    data_stream=train_stream,
    algorithm=algorithm,
    extensions=extensions)

print "Training starting:"
main_loop.run()
