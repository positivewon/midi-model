import argparse
import glob
import json
import time
from typing import Union, Optional

import gradio as gr
import numpy as np
import torch

import torch.nn.functional as F
import tqdm

import MIDI
from midi_model import MIDIModel, config_name_list, MIDIModelConfig
from midi_tokenizer import MIDITokenizerV1, MIDITokenizerV2
from midi_synthesizer import MidiSynthesizer
from huggingface_hub import hf_hub_download

MAX_SEED = np.iinfo(np.int32).max


@torch.inference_mode()
def generate(prompt=None, max_len=512, temp=1.0, top_p=0.98, top_k=20,
             disable_patch_change=False, disable_control_change=False, disable_channels=None, generator=None):
    if disable_channels is not None:
        disable_channels = [tokenizer.parameter_ids["channel"][c] for c in disable_channels]
    else:
        disable_channels = []
    max_token_seq = tokenizer.max_token_seq
    if prompt is None:
        input_tensor = torch.full((1, max_token_seq), tokenizer.pad_id, dtype=torch.long, device=model.device)
        input_tensor[0, 0] = tokenizer.bos_id  # bos
    else:
        prompt = prompt[:, :max_token_seq]
        if prompt.shape[-1] < max_token_seq:
            prompt = np.pad(prompt, ((0, 0), (0, max_token_seq - prompt.shape[-1])),
                            mode="constant", constant_values=tokenizer.pad_id)
        input_tensor = torch.from_numpy(prompt).to(dtype=torch.long, device=model.device)
    input_tensor = input_tensor.unsqueeze(0)
    cur_len = input_tensor.shape[1]
    bar = tqdm.tqdm(desc="generating", total=max_len - cur_len)
    with bar:
        while cur_len < max_len:
            end = False
            hidden = model.forward(input_tensor)[0, -1].unsqueeze(0)
            next_token_seq = None
            event_name = ""
            for i in range(max_token_seq):
                mask = torch.zeros(tokenizer.vocab_size, dtype=torch.int64, device=model.device)
                if i == 0:
                    mask_ids = list(tokenizer.event_ids.values()) + [tokenizer.eos_id]
                    if disable_patch_change:
                        mask_ids.remove(tokenizer.event_ids["patch_change"])
                    if disable_control_change:
                        mask_ids.remove(tokenizer.event_ids["control_change"])
                    mask[mask_ids] = 1
                else:
                    param_name = tokenizer.events[event_name][i - 1]
                    mask_ids = tokenizer.parameter_ids[param_name]
                    if param_name == "channel":
                        mask_ids = [i for i in mask_ids if i not in disable_channels]
                    mask[mask_ids] = 1
                logits = model.forward_token(hidden, next_token_seq)[:, -1:]
                scores = torch.softmax(logits / temp, dim=-1) * mask
                sample = model.sample_top_p_k(scores, top_p, top_k, generator=generator)
                if i == 0:
                    next_token_seq = sample
                    eid = sample.item()
                    if eid == tokenizer.eos_id:
                        end = True
                        break
                    event_name = tokenizer.id_events[eid]
                else:
                    next_token_seq = torch.cat([next_token_seq, sample], dim=1)
                    if len(tokenizer.events[event_name]) == i:
                        break
            if next_token_seq.shape[1] < max_token_seq:
                next_token_seq = F.pad(next_token_seq, (0, max_token_seq - next_token_seq.shape[1]),
                                       "constant", value=tokenizer.pad_id)
            next_token_seq = next_token_seq.unsqueeze(1)
            input_tensor = torch.cat([input_tensor, next_token_seq], dim=1)
            cur_len += 1
            bar.update(1)
            yield next_token_seq.reshape(-1).cpu().numpy()
            if end:
                break


def create_msg(name, data):
    return {"name": name, "data": data}


def send_msgs(msgs):
    return json.dumps(msgs)


def run(tab, mid_seq, continuation_state, instruments, drum_kit, bpm, time_sig, key_sig, mid, midi_events,
        reduce_cc_st, remap_track_channel, add_default_instr, remove_empty_channels, seed, seed_rand,
        gen_events, temp, top_p, top_k, allow_cc):
    bpm = int(bpm)
    if time_sig == "auto":
        time_sig = None
        time_sig_nn = 4
        time_sig_dd = 2
    else:
        time_sig_nn, time_sig_dd = time_sig.split('/')
        time_sig_nn = int(time_sig_nn)
        time_sig_dd = {2: 1, 4: 2, 8: 3}[int(time_sig_dd)]
    if key_sig == 0:
        key_sig = None
        key_sig_sf = 0
        key_sig_mi = 0
    else:
        key_sig = (key_sig - 1)
        key_sig_sf = key_sig // 2 - 7
        key_sig_mi = key_sig % 2
    gen_events = int(gen_events)
    max_len = gen_events
    if seed_rand:
        seed = np.random.randint(0, MAX_SEED)
    generator = torch.Generator(opt.device).manual_seed(seed)
    disable_patch_change = False
    disable_channels = None
    if tab == 0:
        i = 0
        mid = [[tokenizer.bos_id] + [tokenizer.pad_id] * (tokenizer.max_token_seq - 1)]
        if tokenizer.version == "v2":
            if time_sig is not None:
                mid.append(tokenizer.event2tokens(["time_signature", 0, 0, 0, time_sig_nn - 1, time_sig_dd - 1]))
            if key_sig is not None:
                mid.append(tokenizer.event2tokens(["key_signature", 0, 0, 0, key_sig_sf + 7, key_sig_mi]))
        if bpm != 0:
            mid.append(tokenizer.event2tokens(["set_tempo", 0, 0, 0, bpm]))
        patches = {}
        if instruments is None:
            instruments = []
        for instr in instruments:
            patches[i] = patch2number[instr]
            i = (i + 1) if i != 8 else 10
        if drum_kit != "None":
            patches[9] = drum_kits2number[drum_kit]
        for i, (c, p) in enumerate(patches.items()):
            mid.append(tokenizer.event2tokens(["patch_change", 0, 0, i + 1, c, p]))
        mid_seq = mid
        mid = np.asarray(mid, dtype=np.int64)
        if len(instruments) > 0:
            disable_patch_change = True
            disable_channels = [i for i in range(16) if i not in patches]
    elif tab == 1 and mid is not None:
        eps = 4 if reduce_cc_st else 0
        mid = tokenizer.tokenize(MIDI.midi2score(mid), cc_eps=eps, tempo_eps=eps,
                                 remap_track_channel=remap_track_channel,
                                 add_default_instr=add_default_instr,
                                 remove_empty_channels=remove_empty_channels)
        mid = np.asarray(mid, dtype=np.int64)
        mid = mid[:int(midi_events)]
        mid_seq = []
        for token_seq in mid:
            mid_seq.append(token_seq.tolist())
    elif tab == 2 and mid_seq is not None:
        continuation_state.append(len(mid_seq))
        mid = np.asarray(mid_seq, dtype=np.int64)
    else:
        continuation_state = [0]
        mid_seq = []
        mid = None

    if mid is not None:
        max_len += len(mid)

    events = [tokenizer.tokens2event(tokens) for tokens in mid_seq]
    init_msgs = [create_msg("progress", [0, gen_events])]
    if tab != 2:
        init_msgs += [create_msg("visualizer_clear", tokenizer.version),
                     create_msg("visualizer_append", events)]
    yield mid_seq, continuation_state, None, None, seed, send_msgs(init_msgs)
    ctx = torch.amp.autocast(device_type=opt.device, dtype=torch.bfloat16, enabled=opt.device != "cpu")
    with ctx:
        midi_generator = generate(mid, max_len=max_len, temp=temp, top_p=top_p, top_k=top_k,
                                  disable_patch_change=disable_patch_change, disable_control_change=not allow_cc,
                                  disable_channels=disable_channels, generator=generator)
        events = []
        t = time.time()
        for i, token_seq in enumerate(midi_generator):
            token_seq = token_seq.tolist()
            mid_seq.append(token_seq)
            events.append(tokenizer.tokens2event(token_seq))
            ct = time.time()
            if ct - t > 0.2:
                yield (mid_seq, continuation_state, None, None, seed,
                       send_msgs([create_msg("visualizer_append", events),
                                  create_msg("progress", [i + 1, gen_events])]))
                t = ct
                events = []

    events = [tokenizer.tokens2event(tokens) for tokens in mid_seq]
    mid = tokenizer.detokenize(mid_seq)
    audio = synthesizer.synthesis(MIDI.score2opus(mid))
    with open(f"output.mid", 'wb') as f:
        f.write(MIDI.score2midi(mid))
    end_msgs = [create_msg("visualizer_clear", tokenizer.version),
                create_msg("visualizer_append", events),
                create_msg("visualizer_end", None),
                create_msg("progress", [0, 0])]
    yield mid_seq, continuation_state, "output.mid", (44100, audio), seed, send_msgs(end_msgs)


def cancel_run(mid_seq):
    if mid_seq is None:
        return None, None, send_msgs([])
    events = [tokenizer.tokens2event(tokens) for tokens in mid_seq]
    mid = tokenizer.detokenize(mid_seq)
    audio = synthesizer.synthesis(MIDI.score2opus(mid))
    with open(f"output.mid", 'wb') as f:
        f.write(MIDI.score2midi(mid))
    end_msgs = [create_msg("visualizer_clear", tokenizer.version),
                create_msg("visualizer_append", events),
                create_msg("visualizer_end", None),
                create_msg("progress", [0, 0])]
    return "output.mid", (44100, audio), send_msgs(end_msgs)


def undo_continuation(mid_seq, continuation_state):
    if mid_seq is None or len(continuation_state) < 2:
        return mid_seq, continuation_state, send_msgs([])
    mid_seq = mid_seq[:continuation_state[-1]]
    continuation_state = continuation_state[:-1]
    events = [tokenizer.tokens2event(tokens) for tokens in mid_seq]
    end_msgs = [create_msg("visualizer_clear", tokenizer.version),
                create_msg("visualizer_append", events),
                create_msg("visualizer_end", None),
                create_msg("progress", [0, 0])]
    return mid_seq, continuation_state, send_msgs(end_msgs)


def load_model(path, model_config):
    global model, tokenizer
    model = MIDIModel(config=MIDIModelConfig.from_name(model_config))
    tokenizer = model.tokenizer
    ckpt = torch.load(path, map_location="cpu")
    state_dict = ckpt.get("state_dict", ckpt)
    model.load_state_dict(state_dict, strict=False)
    model.to(opt.device, dtype=torch.bfloat16 if opt.device == "cuda" else torch.float32).eval()
    return "success"


def get_model_path():
    model_paths = sorted(glob.glob("**/*.ckpt", recursive=True))
    return gr.Dropdown(choices=model_paths)


def load_javascript(dir="javascript"):
    scripts_list = glob.glob(f"{dir}/*.js")
    javascript = ""
    for path in scripts_list:
        with open(path, "r", encoding="utf8") as jsfile:
            javascript += f"\n<!-- {path} --><script>{jsfile.read()}</script>"
    template_response_ori = gr.routes.templates.TemplateResponse

    def template_response(*args, **kwargs):
        res = template_response_ori(*args, **kwargs)
        res.body = res.body.replace(
            b'</head>', f'{javascript}</head>'.encode("utf8"))
        res.init_headers()
        return res

    gr.routes.templates.TemplateResponse = template_response


number2drum_kits = {-1: "None", 0: "Standard", 8: "Room", 16: "Power", 24: "Electric", 25: "TR-808", 32: "Jazz",
                    40: "Blush", 48: "Orchestra"}
patch2number = {v: k for k, v in MIDI.Number2patch.items()}
drum_kits2number = {v: k for k, v in number2drum_kits.items()}
key_signatures = ['C♭', 'A♭m', 'G♭', 'E♭m', 'D♭', 'B♭m', 'A♭', 'Fm', 'E♭', 'Cm', 'B♭', 'Gm', 'F', 'Dm',
                  'C', 'Am', 'G', 'Em', 'D', 'Bm', 'A', 'F♯m', 'E', 'C♯m', 'B', 'G♯m', 'F♯', 'D♯m', 'C♯', 'A♯m']

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7860, help="gradio server port")
    parser.add_argument("--device", type=str, default="cuda", help="device to run model")
    parser.add_argument("--share", action="store_true", default=False, help="share gradio")
    opt = parser.parse_args()
    soundfont_path = hf_hub_download(repo_id="skytnt/midi-model", filename="soundfont.sf2")
    synthesizer = MidiSynthesizer(soundfont_path)
    tokenizer: Union[MIDITokenizerV1, MIDITokenizerV2, None] = None
    model: Optional[MIDIModel] = None

    load_javascript()
    app = gr.Blocks()
    with app:
        js_msg_history_state = gr.State(value=[])
        js_msg = gr.Textbox(elem_id="msg_receiver", visible=False)
        js_msg.change(None, [js_msg], [], js="""
                (msg_json) =>{
                    let msgs = JSON.parse(msg_json);
                    executeCallbacks(msgReceiveCallbacks, msgs);
                    return [];
                }
                """)
        with gr.Accordion(label="Model option", open=True):
            load_model_path_btn = gr.Button("Get Models")
            model_path_input = gr.Dropdown(label="model")
            model_config_input = gr.Dropdown(label="config", choices=config_name_list, value=config_name_list[0])
            load_model_path_btn.click(get_model_path, [], model_path_input)
            load_model_btn = gr.Button("Load")
            model_msg = gr.Textbox()
            load_model_btn.click(
                load_model, [model_path_input, model_config_input], model_msg
            )
        tab_select = gr.State(value=0)
        with gr.Tabs():
            with gr.TabItem("custom prompt") as tab1:
                input_instruments = gr.Dropdown(label="🪗instruments (auto if empty)", choices=list(patch2number.keys()),
                                                multiselect=True, max_choices=15, type="value")
                input_drum_kit = gr.Dropdown(label="🥁drum kit", choices=list(drum_kits2number.keys()), type="value",
                                             value="None")
                input_bpm = gr.Slider(label="BPM (beats per minute, auto if 0)", minimum=0, maximum=255,
                                      step=1,
                                      value=0)
                input_time_sig = gr.Radio(label="time signature (only for tv2 models)",
                                             value="auto",
                                             choices=["auto", "4/4", "2/4", "3/4", "6/4", "7/4",
                                                      "2/2", "3/2", "4/2", "3/8", "5/8", "6/8", "7/8", "9/8", "12/8"]
                                             )
                input_key_sig = gr.Radio(label="key signature (only for tv2 models)",
                                            value="auto",
                                            choices=["auto"] + key_signatures,
                                            type="index"
                                            )
                example1 = gr.Examples([
                    [[], "None"],
                    [["Acoustic Grand"], "None"],
                    [['Acoustic Grand', 'SynthStrings 2', 'SynthStrings 1', 'Pizzicato Strings',
                      'Pad 2 (warm)', 'Tremolo Strings', 'String Ensemble 1'], "Orchestra"],
                    [['Trumpet', 'Oboe', 'Trombone', 'String Ensemble 1', 'Clarinet',
                      'French Horn', 'Pad 4 (choir)', 'Bassoon', 'Flute'], "None"],
                    [['Flute', 'French Horn', 'Clarinet', 'String Ensemble 2', 'English Horn', 'Bassoon',
                      'Oboe', 'Pizzicato Strings'], "Orchestra"],
                    [['Electric Piano 2', 'Lead 5 (charang)', 'Electric Bass(pick)', 'Lead 2 (sawtooth)',
                      'Pad 1 (new age)', 'Orchestra Hit', 'Cello', 'Electric Guitar(clean)'], "Standard"],
                    [["Electric Guitar(clean)", "Electric Guitar(muted)", "Overdriven Guitar", "Distortion Guitar",
                      "Electric Bass(finger)"], "Standard"]
                ], [input_instruments, input_drum_kit])
            with gr.TabItem("midi prompt") as tab2:
                input_midi = gr.File(label="input midi", file_types=[".midi", ".mid"], type="binary")
                input_midi_events = gr.Slider(label="use first n midi events as prompt", minimum=1, maximum=512,
                                              step=1,
                                              value=128)
                input_reduce_cc_st = gr.Checkbox(label="reduce control_change and set_tempo events", value=True)
                input_remap_track_channel = gr.Checkbox(
                    label="remap tracks and channels so each track has only one channel and in order", value=True)
                input_add_default_instr = gr.Checkbox(
                    label="add a default instrument to channels that don't have an instrument", value=True)
                input_remove_empty_channels = gr.Checkbox(label="remove channels without notes", value=False)
            with gr.TabItem("last output prompt") as tab3:
                gr.Markdown("Continue generating on the last output. Just click the generate button")
                undo_btn = gr.Button("undo the last continuation")

        tab1.select(lambda: 0, None, tab_select, queue=False)
        tab2.select(lambda: 1, None, tab_select, queue=False)
        tab3.select(lambda: 2, None, tab_select, queue=False)
        input_seed = gr.Slider(label="seed", minimum=0, maximum=2 ** 31 - 1,
                               step=1, value=0)
        input_seed_rand = gr.Checkbox(label="random seed", value=True)
        input_gen_events = gr.Slider(label="generate max n midi events", minimum=1, maximum=4096,
                                     step=1, value=512)
        with gr.Accordion("options", open=False):
            input_temp = gr.Slider(label="temperature", minimum=0.1, maximum=1.2, step=0.01, value=1)
            input_top_p = gr.Slider(label="top p", minimum=0.1, maximum=1, step=0.01, value=0.98)
            input_top_k = gr.Slider(label="top k", minimum=1, maximum=128, step=1, value=12)
            input_allow_cc = gr.Checkbox(label="allow midi cc event", value=True)
            example3 = gr.Examples([[1, 0.93, 128], [1, 0.98, 20], [1, 0.98, 12]],
                                   [input_temp, input_top_p, input_top_k])
        run_btn = gr.Button("generate", variant="primary")
        stop_btn = gr.Button("stop and output")
        output_midi_seq = gr.State()
        output_continuation_state = gr.State([0])
        output_midi_visualizer = gr.HTML(elem_id="midi_visualizer_container")
        output_audio = gr.Audio(label="output audio", format="mp3", elem_id="midi_audio")
        output_midi = gr.File(label="output midi", file_types=[".mid"])
        run_event = run_btn.click(run, [tab_select, output_midi_seq, output_continuation_state, input_instruments,
                                        input_drum_kit, input_bpm,  input_time_sig, input_key_sig, input_midi,
                                        input_midi_events, input_reduce_cc_st, input_remap_track_channel,
                                        input_add_default_instr, input_remove_empty_channels, input_seed,
                                        input_seed_rand, input_gen_events, input_temp, input_top_p, input_top_k,
                                        input_allow_cc],
                                  [output_midi_seq, output_continuation_state,
                                   output_midi, output_audio, input_seed, js_msg],
                                  concurrency_limit=3)
        stop_btn.click(cancel_run, [output_midi_seq], [output_midi, output_audio, js_msg], cancels=run_event,
                       queue=False)
        undo_btn.click(undo_continuation, [output_midi_seq, output_continuation_state],
                            [output_midi_seq, output_continuation_state, js_msg], queue=False)
    app.launch(server_port=opt.port, inbrowser=True, share=opt.share)
