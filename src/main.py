import os
import argparse
from pprint import pprint
from types import MethodType

def is_notebook() -> bool:
    try:
        shell = get_ipython().__class__.__name__
        if shell == 'ZMQInteractiveShell' or shell == 'google.colab._shell':
            return True   # Jupyter notebook or qtconsole
        elif shell == 'TerminalInteractiveShell':
            return False  # Terminal running IPython
        else:
            return True  # Other type (?)
    except NameError:
        return False      # Probably standard Python interpreter

if is_notebook():
    from tqdm.notebook import tqdm, trange
else:
    from tqdm import tqdm, trange

from functools import partialmethod
from dataclasses import dataclass
from pathlib import Path

import multiprocessing
import concurrent.futures as futures

import torch
import numpy as np
import whisper

import align
from huggingface import modify_model
from quantization import ptdq_linear
from faster_whisper import WhisperModel

import ffmpeg
from ebooklib import epub
# from fuzzywuzzy import fuzz
from rapidfuzz import fuzz
from tabulate import tabulate, SEPARATING_LINE

from bs4 import element
from bs4 import BeautifulSoup

from os.path import basename, splitext
# from test import astar
from Bio import Align
# from stringzilla import edit_distance


def sexagesimal(secs):
    mm, ss = divmod(secs, 60)
    hh, mm = divmod(mm, 60)
    return f'{hh:0>2.0f}:{mm:0>2.0f}:{ss:0>6.3f}'

@dataclass(eq=True)
class Segment:
    text: str
    # words: Segment
    start: float
    end: float
    def __repr__(self):
        return f"Segment(text='{self.text}', start={sexagesimal(self.start)}, end={sexagesimal(self.end)})"
    def vtt(self):
        return f"{sexagesimal(self.start)} --> {sexagesimal(self.end)}\n{self.text}"

@dataclass(eq=True)
class Cache:
    model_name: str
    cache_dir: str
    enabled: bool
    ask: bool
    overwrite: bool
    memcache: dict

    def get(self, filename, chid):
        if not self.enabled: return
        if filename in self.memcache: return self.memcache[filename]
        fn = (filename + '.' + str(chid) +  '.' + self.model_name + ".subs") # Include the hash of the model settings?
        fn2 = (filename + '.' + str(chid) +  '.' + 'small' + ".subs") # TODO(YM): DEBUG
        if (q := Path(self.cache_dir) / fn2).exists():
            return eval(q.read_bytes().decode("utf-8"))
        if (q := Path(self.cache_dir) / fn).exists():
            return eval(q.read_bytes().decode("utf-8"))

    def put(self, filename, chid, content):
        # if not self.enabled: return content
        cd = Path(self.cache_dir)
        cd.mkdir(parents=True, exist_ok=True)
        q = cd / (filename + '.' + str(chid) +  '.' + self.model_name + ".subs")
        if q.exists():
            if self.ask:
                prompt = f"Cache for file {filename}, chapter id {chid} already exists. Overwrite?  [y/n/Y/N] (yes, no, yes/no and don't ask again) "
                while (k := input(prompt).strip()) not in ['y', 'n', 'Y', 'N']: pass
                self.ask = not (k == 'N' or k == 'Y')
                self.overwrite = k == 'Y' or k == 'y'
            if not self.overwrite: return content

        if 'text' in content:
            del content['text']
        if 'ori_dict' in content:
            del content['ori_dict']

        # Some of these may be useful but they just take so much space
        for i in content['segments']:
            if 'words' in i:
                del i['words']
            del i['id']
            del i['tokens']
            del i['avg_logprob']
            del i['temperature']
            del i['seek']
            del i['compression_ratio']
            del i['no_speech_prob']

        self.memcache[q] = content
        q.write_bytes(repr(content).encode('utf-8'))
        return content

@dataclass(eq=True, frozen=True)
class AudioStream:
    stream: ffmpeg.Stream
    path: Path
    duration: float
    cn: str
    cid: int

    def audio(self):
        data, _ = self.stream.output('-', format='s16le', acodec='pcm_s16le', ac=1, ar='16k').run(quiet=True, input='')
        return np.frombuffer(data, np.int16).astype(np.float32) / 32768.0

    def transcribe(self, model, cache, **kwargs):
        transcription = cache.get(os.path.basename(self.path), self.cid)
        if transcription is not None:
            return transcription
        transcription = model.transcribe(self.audio(), name=self.cn, **kwargs)
        return cache.put(os.path.basename(self.path), self.cid, transcription)

    @classmethod
    def from_file(cls, path, whole=False):
        print(path)
        info = ffmpeg.probe(path, show_chapters=None)
        title = info.get('format', {}).get('tags', {}).get('title', os.path.basename(path))
        if whole or 'chapters' not in info or len(info['chapters']) < 1:
            return title, [cls(stream=ffmpeg.input(path), transcription=None, duration=float(info['streams'][0]['duration']), path=path, cn=title, cid=0)]
        return title, [cls(stream=ffmpeg.input(path, ss=float(chapter['start_time']), to=float(chapter['end_time'])),
                           duration=float(chapter['end_time']) - float(chapter['start_time']),
                           path=path,
                           cn=chapter.get('tags', {}).get('title', ''),
                           cid=chapter['id'])
                       for chapter in info['chapters']]

@dataclass(eq=True, frozen=True)
class Paragraph:
    chapter: int
    element: element.Tag
    references: list

    def text(self):
        return ''.join(self.element.stripped_strings)


@dataclass(eq=True, frozen=True)
class TextParagraph:
    path: str
    idx: int
    content: str
    references: list

    def text(self):
        return self.content

@dataclass(eq=True, frozen=True)
class TextFile:
    path: str
    title: str
    def text(self, *args, **kwargs):
        return [TextParagraph(path=self.path, idx=i, content=o, references=[]) for i, v in enumerate(Path(self.path).read_text().split('\n')) if (o := v.strip()) != '']

@dataclass(eq=True, frozen=True)
class Epub:
    epub: epub.EpubBook
    title: str
    start: int
    end: int

    def text(self, prefix=None, follow_links=True, ignore=set()):
        o = []
        refids = set()
        for i in range(self.start, self.end):
            id, is_linear = self.epub.spine[i]
            item = self.epub.get_item_with_id(id)
            # https://gitlab.com/smoores/storyteller/-/blob/main/storyteller/synchronize/epub.py?ref_type=heads#L259
            if is_linear and item.media_type == "application/xhtml+xml":
                soup = BeautifulSoup(item.get_content(), 'html.parser')
                paragraphs = soup.find("body").find_all(["p", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"])

                for p in paragraphs:
                    if 'id' in p.attrs and p.attrs['id'] in refids:
                        continue
                    references = []
                    for r in p.find_all(href=True):
                        if "#" not in r['href']: continue
                        path, tid = r['href'].split("#")

                        if os.path.basename(path) != os.path.basename(item.file_name):
                            chapter = self.epub.get_item_with_href(path if '/' in path else (os.path.dirname(item.file_name) + '/' + path))
                            idx = [i for i, _ in self.epub.spine].index(chapter.id)
                            ref = BeautifulSoup(chapter.get_content(), 'html.parser').find(id=tid) # TODO: cache or whatever and get rid of this if
                        else:
                            idx = i
                            ref = soup.find(id=tid)
                        refids.add(tid)
                        references.append(Paragraph(chapter=idx, element=ref, references=[]))
                    o.append(Paragraph(chapter=i, element=p, references=references))
        return o

    @classmethod
    def from_file(cls, path):
        file = epub.read_epub(path, {"ignore_ncx": True})
        toc = [file.get_item_with_href(x.href.split("#")[0]) for x in file.toc]

        idx, c, k = [], 0, 0
        while len(toc) > c:
            for i in range(idx[-1]+1 if len(idx) else 0, len(file.spine)):
                v = file.spine[i]
                if v[0] == toc[c].id:
                    idx.append(i)
                    c += 1
                    if c == len(toc): break
            idx.append(idx[-1])
            c += 1
            k += 1
        if k > 1: print(file.title, "has a broken toc")

        idx[-1] = len(file.spine)
        return [cls(epub=file, title=file.toc[i].title, start=idx[i], end=idx[i+1]) for i in range(len(toc))]

def match(audio, text):
    ats, sta = {}, {}
    picked = set()
    for ai in range(len(audio)):
        afn, at, ac = audio[ai]
        audio_full_title = align.clean(afn+at)
        for i in range(len(ac)):
            ach = audio_full_title + align.clean(ac[i].cn)
            best = (-1, -1, 0)
            for ti in range(len(text)):
                tfn, tc = text[ti]
                if type(tc[0]) is not Epub: continue

                text_full_title = align.clean(tfn + tc[0].epub.title)
                main = fuzz.ratio(audio_full_title, text_full_title)

                for j in range(len(tc)):
                    tch = text_full_title + align.clean(tc[j].title)
                    # print(ach, tch)
                    score = fuzz.ratio(ach, tch)
                    if score > main and score > best[-1]:
                        best = (ti, j, score)

            if best[:-1] in picked:
                key = best[:-1]
                if key in sta:
                    ats.pop(sta.pop(key)[:-1])
            elif best != (-1, -1, 0):
                ats[(ai, i)] = best
                sta[best[:-1]] = (ai, i, best[-1])
                picked.add(best[:-1])

    return ats, sta

# THIS IS SUPER SLOW LOOOOL
def content_match(audio, text, ats, sta, cache):
    aligner = Align.PairwiseAligner(scoring=None, mode='global', match_score=1, open_gap_score=-1, mismatch_score=-1, extend_gap_score=-1)
    picked = set()
    textcache = {}
    for ai in trange(len(audio)):
        afn, at, ac = audio[ai]
        for i in trange(len(ac)):
            if (ai, i) in ats: continue

            acontent = align.clean(''.join(seg['text'] for seg in ac[i].transcribe(None, cache)['segments']))
            best = (-1, -1, 0)
            for ti in range(len(text)):
                tfn, tc = text[ti]

                for j in range(len(tc)):
                    # if (ti, j) not in picked and (ti, j) in sta: continue # Allow dupes
                    if (ti, j) in sta: continue

                    if (ti, j) not in textcache:
                        textcache[(ti, j)] = align.clean(''.join(p.text() for p in tc[j].text()))
                        # if len(textcache[(ti, j)]) == 6301:
                        #     open("/tmp/test", "w").write(textcache[(ti, j)])
                        #     open("/tmp/test2", "w").write(acontent)
                        #     exit(0)
                    tcontent = textcache[(ti, j)]


                    if len(acontent) < 5 or len(tcontent) < 5:
                        continue

                    # score =  aligner.align(acontent, tcontent).score / max(len(acontent), len(tcontent)) * 50 + 50 # astar(acontent, tcontent) #fuzz.ratio(acontent, tcontent)
                    score = fuzz.ratio(acontent, tcontent)
                    if score > 40 and score > best[-1]:
                        best = (ti, j, score)

            if best != (-1, -1, 0):
                ats[(ai, i)] = best
                sta[best[:-1]] = (ai, i, best[-1])
                picked.add(best[:-1])

    return ats, sta

def to_epub():
    pass

def to_subs(text, subs, alignment, offset, references):
    alignment = [t + [i] for i, a in enumerate(alignment[:-2]) for t in a]
    start, end = 0, 0
    segments = []
    for si, s in enumerate(subs['segments']):
        while end < len(alignment) and alignment[end][-2] == si:
            end += 1

        r = ''
        for a in alignment[start:end]:
            r += text[a[-1]].text()[a[0]:a[1]]

        if r.strip():
            segments.append(Segment(text=r, start=s['start']+offset, end=s['end']+offset))
        else:
            segments.append(Segment(text='＊'+s['text'], start=s['start']+offset, end=s['end']+offset))

        start = end
    return segments

def faster_transcribe(self, audio, **args):
    name = args.pop('name')

    args['log_prob_threshold'] = args.pop('logprob_threshold')
    args['beam_size'] = args['beam_size'] if args['beam_size'] else 1
    args['patience'] = args['patience'] if args['patience'] else 1
    args['length_penalty'] = args['length_penalty'] if args['length_penalty'] else 1

    gen, info = self.transcribe2(audio, best_of=1, **args)

    segments, prev_end = [], 0
    with tqdm(total=info.duration, unit_scale=True, unit=" seconds") as pbar:
        pbar.set_description(f'{name}')
        for segment in gen:
            segments.append(segment._asdict())
            pbar.update(segment.end - prev_end)
            prev_end = segment.end
        pbar.update(info.duration - prev_end)
        pbar.refresh()

    return {'segments': segments, 'language': args['language'] if 'language' in args else info.language}

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Match audio to a transcript")
    parser.add_argument( "--audio", nargs="+", required=True, help="list of audio files to process (in the correct order)")
    parser.add_argument("--text", nargs="+", required=True, help="path to the script file")
    parser.add_argument("--model", default="tiny", help="whisper model to use. can be one of tiny, small, large, huge")
    parser.add_argument("--language", default=None, help="language of the script and audio")
    parser.add_argument("--progress", default=True,  help="progress bar on/off", action=argparse.BooleanOptionalAction)
    parser.add_argument("--use-cache", default=True, help="whether to use the cache or not", action=argparse.BooleanOptionalAction)
    parser.add_argument("--cache-dir", default="AudiobookTextSyncCache", help="the cache directory")
    parser.add_argument("--overwrite-cache", default=False, action=argparse.BooleanOptionalAction, help="Always overwrite the cache")
    parser.add_argument("--threads", type=int, default=multiprocessing.cpu_count(), help=r"number of threads")
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu", help="device to do inference on")
    parser.add_argument("--dynamic-quantization", "--dq", default=False, help="Use torch's dynamic quantization (cpu only)", action=argparse.BooleanOptionalAction)

    parser.add_argument("--faster-whisper", default=True, help='Use faster_whisper, doesn\'t work with hugging face\'s decoding method currently', action=argparse.BooleanOptionalAction)
    parser.add_argument("--fast-decoder", default=False, help="Use hugging face's decoding method, currently incomplete", action=argparse.BooleanOptionalAction)
    parser.add_argument("--fast-decoder-overlap", type=int, default=10,help="Overlap between each batch")
    parser.add_argument("--fast-decoder-batches", type=int, default=1, help="Number of batches to operate on")

    parser.add_argument("--ignore-tags", default=['rt'], nargs='+', help="Tags to ignore during the epub to text conversion, useful for removing furigana")
    parser.add_argument("--prefix-chapter-name", default=True, help="Whether to prefix the text of each chapter with its name", action=argparse.BooleanOptionalAction)
    parser.add_argument("--follow-links", default=True, help="Whether to follow hrefs or not in the ebook", action=argparse.BooleanOptionalAction)

    parser.add_argument("--beam_size", type=int, default=None, help="number of beams in beam search, only applicable when temperature is zero")
    parser.add_argument("--patience", type=float, default=None, help="optional patience value to use in beam decoding, as in https://arxiv.org/abs/2204.05424, the default (1.0) is equivalent to conventional beam search")
    parser.add_argument("--length_penalty", type=float, default=None, help="optional token length penalty coefficient (alpha) as in https://arxiv.org/abs/1609.08144, uses simple length normalization by default")

    parser.add_argument("--suppress_tokens", type=str, default=[-1], help="comma-separated list of token ids to suppress during sampling; '-1' will suppress most special characters except common punctuations")
    parser.add_argument("--initial_prompt", type=str, default=None, help="optional text to provide as a prompt for the first window.")
    parser.add_argument("--condition_on_previous_text", default=False, help="if True, provide the previous output of the model as a prompt for the next window; disabling may make the text inconsistent across windows, but the model becomes less prone to getting stuck in a failure loop", action=argparse.BooleanOptionalAction)

    parser.add_argument("--temperature", type=float, default=0, help="temperature to use for sampling")
    parser.add_argument("--temperature_increment_on_fallback", type=float, default=0.2, help="temperature to increase when falling back when the decoding fails to meet either of the thresholds below")
    parser.add_argument("--compression_ratio_threshold", type=float, default=2.4, help="if the gzip compression ratio is higher than this value, treat the decoding as failed")
    parser.add_argument("--logprob_threshold", type=float, default=-1.0, help="if the average log probability is lower than this value, treat the decoding as failed")
    parser.add_argument("--no_speech_threshold", type=float, default=0.6, help="if the probability of the <|nospeech|> token is higher than this value AND the decoding has failed due to `logprob_threshold`, consider the segment as silence")
    parser.add_argument("--word_timestamps", default=False, help="(experimental) extract word-level timestamps and refine the results based on them", action=argparse.BooleanOptionalAction)
    parser.add_argument("--prepend_punctuations", type=str, default="\"\'“¿([{-『「（〈《〔【｛［‘“〝※", help="if word_timestamps is True, merge these punctuation symbols with the next word")
    parser.add_argument("--append_punctuations", type=str, default="\"\'・.。,，!！?？:：”)]}、』」）〉》〕】｝］’〟／＼～〜~", help="if word_timestamps is True, merge these punctuation symbols with the previous word")
    parser.add_argument("--nopend_punctuations", type=str, default="うぁぃぅぇぉっゃゅょゎゕゖァィゥェォヵㇰヶㇱㇲッㇳㇴㇵㇶㇷㇷ゚ㇸㇹㇺャュョㇻㇼㇽㇾㇿヮ…\u3000\x20", help="TODO")
    parser.add_argument("--highlight_words", default=False, help="(requires --word_timestamps True) underline each word as it is spoken in srt and vtt", action=argparse.BooleanOptionalAction)
    parser.add_argument("--max_line_width", type=int, default=None, help="(requires --word_timestamps True) the maximum number of characters in a line before breaking the line")
    parser.add_argument("--max_line_count", type=int, default=None, help="(requires --word_timestamps True) the maximum number of lines in a segment")
    parser.add_argument("--max_words_per_line", type=int, default=None, help="(requires --word_timestamps True, no effect with --max_line_width) the maximum number of words in a segment")
    parser.add_argument("--output-dir", default=None, help="Output directory, default uses the directory for the first audio file")
    parser.add_argument("--local-only", default=False, help="Don't download outside models", action=argparse.BooleanOptionalAction)
    # parser.add_argument("--split-script", default="", help=r"the regex to split the script with. for monogatari it is something like ^\s[\uFF10-\uFF19]*\s$")

    args = parser.parse_args().__dict__
    tqdm.__init__ = partialmethod(tqdm.__init__, disable=not args.pop('progress'))
    if (threads := args.pop("threads")) > 0: torch.set_num_threads(threads)

    output_dir = Path(k) if (k := args.pop('output_dir')) else Path('.')#os.path.dirname(args['audio'][0]))
    output_dir.mkdir(parents=True, exist_ok=True)

    model, device = args.pop("model"), args.pop('device')

    overwrite_cache = args.pop('overwrite_cache')
    cache = Cache(model_name=model, enabled=args.pop("use_cache"), cache_dir=args.pop("cache_dir"),
                  ask=not overwrite_cache, overwrite=overwrite_cache,
                  memcache={})

    faster_whisper = args.pop('faster_whisper')
    local_only = args.pop('local_only')
    if faster_whisper:
        model = WhisperModel(model, device, local_files_only=local_only, compute_type='int8' if device == 'cpu' else 'float16', num_workers=threads)
        model.transcribe2 = model.transcribe
        model.transcribe = MethodType(faster_transcribe, model)
    else:
        model = whisper.load_model(model).to(device)

    if args.pop('dynamic_quantization') and device == "cpu" and not faster_whisper:
        ptdq_linear(model)

    overlap, batches = args.pop("fast_decoder_overlap"), args.pop("fast_decoder_batches")
    if args.pop("fast_decoder") and not faster_whisper:
        args["overlap"] = overlap
        args["batches"] = batches
        modify_model(model)

    streams = [(os.path.basename(f), *AudioStream.from_file(f)) for f in args.pop('audio')]
    chapters = [(os.path.basename(i), Epub.from_file(i)) if i.split(".")[-1] == 'epub' else (os.path.basename(i), [TextFile(path=i, title=os.path.basename(i))]) for i in args.pop('text')]

    temperature = args.pop("temperature")
    if (increment := args.pop("temperature_increment_on_fallback")) is not None:
        temperature = tuple(np.arange(temperature, 1.0 + 1e-6, increment))
    else:
        temperature = [temperature]

    word_options = [
        "highlight_words",
        "max_line_count",
        "max_line_width",
        "max_words_per_line",
    ]
    if not args["word_timestamps"]:
        for option in word_options:
            if args[option]:
                parser.error(f"--{option} requires --word_timestamps True")

    if args["max_line_count"] and not args["max_line_width"]:
        warnings.warn("--max_line_count has no effect without --max_line_width")
    if args["max_words_per_line"] and args["max_line_width"]:
        warnings.warn("--max_words_per_line has no effect with --max_line_width")
    writer_args = {arg: args.pop(arg) for arg in word_options}
    word_timestamps = args.pop("word_timestamps")

    ignore_tags = set(args.pop('ignore_tags'))
    prefix_chapter_name = args.pop('prefix_chapter_name')
    follow_links = args.pop('follow_links')

    ats, sta = match(streams, chapters)

    nopend = args.pop('nopend_punctuations')

    print('Transcribing...')
    # with tqdm(range(len(streams))) as bar:
    #     for i in bar:
    #         bar.set_description(basename(streams[i][2][0].path))
    #         for j in range(len(streams[i][2])):
    #             streams[i][2][j].transcribe(model, cache, temperature=temperature, **args)

    with futures.ThreadPoolExecutor(max_workers=threads) as p:
        r = []
        for i in range(len(streams)):
            for j, v in enumerate(streams[i][2]):
                r.append(p.submit(lambda x: x.transcribe(model, cache, temperature=temperature, **args), v))
        futures.wait(r)

    print('Fuzzy matching chapters...')
    content_match(streams, chapters, ats, sta, cache)

    h = []
    prev = None
    for k, v in sorted(ats.items(), key=lambda x: x[0]):
        ai, i = k
        ti, tj, s = v
        if prev is not None and ai != prev:
            h.append(SEPARATING_LINE)
        prev = ai
        h.append([streams[ai][1] + ":" + streams[ai][2][i].cn, chapters[ti][1][0].epub.title + ":" + chapters[ti][1][tj].title if type(chapters[ti][1][0]) is Epub else chapters[ti][1][tj].path, s])

    print(tabulate(h, headers=["Audio", "Text", "Score"], tablefmt='rst'))

    print('Syncing...')
    with tqdm(streams) as bar:
        for i, v in enumerate(bar):
            bar.set_description(basename(v[2][0].path))
            offset, segments = 0, []
            with tqdm(v[2]) as bar2:
                for j, audstr in enumerate(bar2):
                    bar2.set_description(audstr.cn)
                    if (i, j) in ats:
                        ci, cj, _ = ats[(i, j)]
                        text = chapters[ci][1][cj].text(prefix_chapter_name, follow_links=follow_links, ignore=ignore_tags)
                        transcript = audstr.transcribe(model, cache, temperature=temperature, **args)
                        alignment, references = align.align(model, transcript, text, set(args['prepend_punctuations']), set(args['append_punctuations']), set(nopend))
                        segments.extend(to_subs(text, transcript, alignment, offset, references))
                    offset += audstr.duration
            if segments:
                with (output_dir / (splitext(basename(v[2][0].path))[0] + '.vtt')).open("w", encoding='utf-8') as out:
                    out.write("WEBVTT\n\n")
                    out.write('\n\n'.join([s.vtt() for s in segments]))
            else:
                print(v[2][0].path, "empty alignment?")
    pprint(align.g_unused)
