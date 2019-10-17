# filename: parlai/agents/my_agent/my_agent.py

from parlai.core.agents import Agent

from collections import namedtuple
from subprocess import PIPE, Popen
import random
import fileinput
import traceback
import os, stat, sys
import torch
import time
from fairseq import checkpoint_utils, options, tasks, utils
from fairseq.data import encoders
from .simple_dialogue import DialogueSimpleTask

print('import okay')

def boolean_string(s):
    if s not in {"False", "True"}:
        raise ValueError("Not a valid boolean string")
    return s == "True"

Batch = namedtuple("Batch", "ids src_tokens src_lengths")
Translation = namedtuple("Translation", "src_str hypos pos_scores alignments")

def make_batches(lines, args, task, max_positions, encode_fn):
    tokens = [
        task.source_dictionary.encode_line(
            encode_fn(src_str), add_if_not_exist=False, append_eos=args.append_eos
        ).long()
        for src_str in lines
    ]
    lengths = torch.LongTensor([t.numel() for t in tokens])
    itr = task.get_batch_iterator(
        dataset=task.build_dataset_for_inference(tokens, lengths),
        max_tokens=args.max_tokens,
        max_sentences=args.max_sentences,
        max_positions=max_positions,
    ).next_epoch_itr(shuffle=False)
    for batch in itr:
        yield Batch(
            ids=batch["id"],
            src_tokens=batch["net_input"]["src_tokens"],
            src_lengths=batch["net_input"]["src_lengths"],
        )

class MyAgentAgent(Agent):
    
    loaded_model = None

    def mose_tokenizer(self, text):
        mose_dir = '/data/sls/u/tianxing/toolkits/mosesdecoder/scripts/tokenizer/'
        print(os.path.join(mose_dir, "normalize-punctuation.perl"))
        process = Popen(
            ["perl", os.path.join(mose_dir, "normalize-punctuation.perl"), "-l", "en"],
            stdout=PIPE,
            stdin=PIPE,
            stderr=PIPE,
            encoding="utf8",
        )
        stdOut, stdErr = process.communicate(text)
        print(stdOut)
        process = Popen(
            [
                "perl",
                os.path.join(mose_dir, "remove-non-printing-char.perl"),
                # "-q",
            ],
            stdout=PIPE,
            stdin=PIPE,
            stderr=PIPE,
            encoding="utf8",
        )
        stdOut, stdErr = process.communicate(stdOut.rstrip())
        print(stdOut)
        process = Popen(
            [
                "perl",
                os.path.join(mose_dir, "tokenizer.perl"),
                "-a",
                "-l",
                "en",
                # "-q",
            ],
            stdout=PIPE,
            stdin=PIPE,
            stderr=PIPE,
            encoding="utf8",
        )
        print(stdOut)
        stdOut, stdErr = process.communicate(stdOut.rstrip())
        return stdOut.strip()

    def apply_bpe(self, text):
        bpe_file = self.bpe_code_fn
        bpe_bin = '/data/sls/u/tianxing/toolkits/fastBPE/fast_bpe'  # fastBPE/bin/fast_bpe'

        st = os.stat(bpe_bin)
        os.chmod(bpe_bin, st.st_mode | stat.S_IEXEC)

        print("doing bpe", bpe_bin + " applybpe_stream " + bpe_file)
        process = Popen(
            [bpe_bin, "applybpe_stream", bpe_file],
            stdout=PIPE,
            stdin=PIPE,
            stderr=PIPE,
            encoding="utf8",
        )
        stdOut, stdErr = process.communicate(text)
        print("result from fast_BPE", stdOut, stdErr)
        return stdOut.strip()

    def __init__(self, opt, shared=None):
        super().__init__(opt, shared)

        print('!!!!! My Agent INI!!!')
        filename_path = opt['model_path']
        dict_fn = '/data/sls/temp/tianxing/data-sets/201908_dialogue_tianxing_fbintern/data-sets/ccnews/ccnews_process/100m_dict.txt'
        self.dict_fn = dict_fn
        self.bpe_code_fn = '/data/sls/temp/tianxing/data-sets/201908_dialogue_tianxing_fbintern/data-sets/ccnews/ccnews_process/100m_bpe.50k.codes'
        input_args = [
            'tmp/',
            "--path",
            filename_path,
            "--dict-file",
            dict_fn,
            "--task",
            "simple_dialogue",
            "--nbest",
            "30",
            "--beam",
            "30",
            "--append-eos",
            "False",
            #"--remove-bpe",
        ]  # , '-s', 'en', '-t', 'en']
        input_args_topk = [
            'tmp/',
            "--path",
            filename_path,
            "--dict-file",
            dict_fn,
            "--task",
            "simple_dialogue",
            "--nbest",
            "30",
            "--beam",
            "30",
            "--append-eos",
            "False",
            "--sampling",
            "--sampling-topk",
            "30",
            #"--remove-bpe",
        ]  # , '-s', 'en', '-t', 'en']
        
        parser = options.get_generation_parser(interactive=True)
        # parser.add_argument('--dict-file', type=str, help='dict file')
        parser.add_argument(
            "--append-eos",
            type=boolean_string,
            default=True,
            help="whether to append eos for the input",
        )
        args = options.parse_args_and_arch(parser, input_args)

        parser_topk = options.get_generation_parser(interactive=True)
        parser_topk.add_argument(
            "--append-eos",
            type=boolean_string,
            default=True,
            help="whether to append eos for the input",
        )

        args_topk = options.parse_args_and_arch(parser_topk, input_args_topk)
        self.args = args
        self.args_topk = args_topk

        if args.buffer_size < 1:
            args.buffer_size = 1
        if args.max_tokens is None and args.max_sentences is None:
            args.max_sentences = 1

        assert (
            not args.sampling or args.nbest == args.beam
        ), "--sampling requires --nbest to be equal to --beam"
        assert (
            not args.max_sentences or args.max_sentences <= args.buffer_size
        ), "--max-sentences/--batch-size cannot be larger than --buffer-size"

        print('fairseq parsed args:', args)
        
        # Setup task, e.g., translation
        task = tasks.setup_task(args)
        
        if MyAgentAgent.loaded_model is not None:
            print('=== model alreadyed loaded!')
            self.models = MyAgentAgent.loaded_model 
        else:
            # Load ensemble
            print("| loading model(s) from {}".format(args.path))
            model_paths = args.path.split(":")
            self.models, self.model_args = utils.load_ensemble_for_inference(
                model_paths, task
            )
            MyAgentAgent.loaded_model = self.models

        # Set dictionaries
        self.src_dict = task.source_dictionary
        self.tgt_dict = task.target_dictionary
        self.args = args
        self.task = task
        print("model loading complete")

        self.use_cuda = True
        models = self.models
        # Optimize ensemble for generation
        for model in models:
            model.make_generation_fast_(
                beamable_mm_beam_size=None if args.no_beamable_mm else args.beam,
                need_attn=args.print_alignment,
            )
            if args.fp16:
                model.half()
            if self.use_cuda:
                print("using cuda!")
                model.cuda()
            else:
                print("not using cuda!")

        # Initialize generator
        self.generator = task.build_generator(args)
        # Initialize generator
        self.generator_topk = task.build_generator(args)

        # Handle tokenization and BPE
        tokenizer = encoders.build_tokenizer(args)
        bpe = encoders.build_bpe(args)

        def encode_fn(x):
            # if tokenizer is not None:
            #    x = tokenizer.encode(x)
            # x = mose_tokenizer(x)
            # if bpe is not None:
            #    x = bpe.encode(x)
            return x

        self.encode_fn = encode_fn

        def decode_fn(x):
            #if bpe is not None:
            #    x = bpe.decode(x)
            if tokenizer is not None:
                x = tokenizer.decode(x)
            x = x.replace("&apos;", "'")
            x = x.replace('@@ ', '')
            x = x.replace("' ", "'")
            x = x.replace(" 'd", " would ")
            x = x.replace("n 't", "n't")
            x = x.replace(" 're ", " are ")
            return x

        self.decode_fn = decode_fn

        # Load alignment dictionary for unknown word replacement
        # (None if no unknown word replacement, empty if no path to align dictionary)
        self.align_dict = utils.load_align_dict(args.replace_unk)

        self.max_positions = utils.resolve_max_positions(
            task.max_positions(), *[model.max_positions() for model in models]
        )

        if shared is None:
            # do any other special initializing here
            self.model = [1] #load_fairseq_magic_code() # load in your model here
        else:
            # put any other stuff you need to share across instantiations here
            self.model = shared['model']
        self.dialogue_history = ''
        print('my_agent INI complete')
        time.sleep(3)

    def reset(self):
        super().reset()
        self.dialogue_history = ''
    
    def share(self):
        # put any other special reusing stuff in shared too
        shared = super().share()
        shared['model'] = self.model
        return shared

    def observe(self, observation):
        print(observation)
        traceback.print_stack(file=sys.stdout)
        # your goal is to build up the string input to the model here
        tt = observation['text'].strip().lower().split()
        text = self.mose_tokenizer(" ".join(tt))
        # text = self.encode_fn(' '.join(tt))
        print("after tok:", text)
        text = self.apply_bpe(text)
        print("after bpe:", text)
        tt = text.split()
        if tt[-1] != "." and tt[-1] != "?" and tt[-1] != "!":
            question = False
            for kk in [
                "am i",
                "do you",
                "do i",
                "does he",
                "does she",
                "are you",
                "what",
                "which",
                "how",
                "why",
                "who",
                "should i",
                "could you",
                "can i",
                # "have you",
                "could i",
                "can you",
                "is that",
                "are they",
                "is it",
            ]:
                if kk in text:
                    question = True
            if tt[0] in ["do", "is", "are", "could", "should", "would"]:
                question = True
            if " ".join(tt) == "really":
                question = True
            if question == True:
                tt.append("?")
            else:
                tt.append(".")
        
        if tt[-1] != "<eou>":
            tt.append("<eou>")

        ts = " ".join(tt)
 
        self.dialogue_history += ' ' + ts
        tt = self.dialogue_history.split()
        print('test length of 128', len(tt))
        if len(tt) > 128:
            tt = tt[-128:]
        self.dialogue_history = ' '.join(tt)
        return observation

    def act(self):
        print('dialogue_history:', self.dialogue_history)
        inputs = [self.dialogue_history]
        # scores = []

        def get_res(mode="beam"):

            start_id = 0
            results = []
            sentences = []

            if mode == "beam":
                args = self.args
                generator = self.generator
            else:
                args = self.args_topk
                generator = self.generator_topk

            for batch in make_batches(
                inputs, args, self.task, self.max_positions, self.encode_fn
            ):
                src_tokens = batch.src_tokens
                src_lengths = batch.src_lengths
                if self.use_cuda:
                    src_tokens = src_tokens.cuda()
                    src_lengths = src_lengths.cuda()

                sample = {
                    "net_input": {"src_tokens": src_tokens, "src_lengths": src_lengths}
                }
                translations = self.task.inference_step(generator, self.models, sample)
                for i, (id, hypos) in enumerate(zip(batch.ids.tolist(), translations)):
                    src_tokens_i = utils.strip_pad(src_tokens[i], self.tgt_dict.pad())
                    results.append((start_id + id, src_tokens_i, hypos))

            # sort output to match input order
            for id, src_tokens, hypos in sorted(results, key=lambda x: x[0]):
                if self.src_dict is not None:
                    src_str = self.src_dict.string(src_tokens, args.remove_bpe)
                    print("S-{}\t{}".format(id, src_str))

                # Process top predictions
                for hypo in hypos[: min(len(hypos), args.nbest)]:
                    hypo_tokens, hypo_str, alignment = utils.post_process_prediction(
                        hypo_tokens=hypo["tokens"].int().cpu(),
                        src_str=src_str,
                        alignment=hypo["alignment"].int().cpu()
                        if hypo["alignment"] is not None
                        else None,
                        align_dict=self.align_dict,
                        tgt_dict=self.tgt_dict,
                        remove_bpe=args.remove_bpe,
                    )
                    # if mode == 'beam':
                    #     ss = 'beam search'
                    # else:
                    #     ss = 'top k sampling'
                    sentences.append(hypo_str)
                    # scores.append(hypo['score'])
                    print("H-{}\t{}\t{}".format(id, hypo["score"], hypo_str))
                    print(
                        "P-{}\t{}".format(
                            id,
                            " ".join(
                                map(
                                    lambda x: "{:.4f}".format(x),
                                    hypo["positional_scores"].tolist(),
                                )
                            ),
                        )
                    )
                    if args.print_alignment:
                        print(
                            "A-{}\t{}".format(
                                id,
                                " ".join(
                                    map(lambda x: str(utils.item(x)), self.alignment)
                                ),
                            )
                        )
            return sentences

        #s_beam = get_res("beam")
        s_topk = get_res("topk")
        random.shuffle(s_topk)
        chosen_one = s_topk[0] 
        self.dialogue_history += ' ' + chosen_one + ' <eou>'

        out_s = self.decode_fn(chosen_one)
        return {
            'text': out_s
        }