#!/usr/bin/env python3

# This file is part of ffplayout.
#
# ffplayout is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# ffplayout is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with ffplayout. If not, see <http://www.gnu.org/licenses/>.

# ------------------------------------------------------------------------------
from platform import system
from queue import Queue
from subprocess import PIPE, Popen
from threading import Thread
from time import sleep

from ..filters.default import overlay_filter
from ..folder import GetSourceFromFolder, MediaStore, MediaWatcher
from ..playlist import GetSourceFromPlaylist
from ..utils import (ff_proc, ffmpeg_stderr_reader, get_date, get_time, ingest,
                     log, lower_third, messenger, playlist, playout, pre,
                     pre_audio_codec, stdin_args, sync_op, terminate_processes)

COPY_BUFSIZE = 1024 * 1024 if system() == 'Windows' else 65424


def rtmp_server(que, pre_settings):
    filter_ = (f'[0:v]fps={str(pre.fps)},scale={pre.w}:{pre.h},'
               + f'setdar=dar={pre.aspect}[v];')
    filter_ += overlay_filter(0, False, False, False)

    server_cmd = [
        'ffmpeg', '-hide_banner', '-nostats', '-v', 'level+error'
    ] + ingest.stream_input + [
        '-filter_complex', f'{filter_}[vout1]',
        '-map', '[vout1]', '-map', '0:a'
    ] + pre_settings

    messenger.warning(
        'Ingest stream is experimental, use it at your own risk!')
    messenger.debug(f'Server CMD: "{" ".join(server_cmd)}"')

    while True:
        with Popen(server_cmd, stderr=PIPE, stdout=PIPE) as ff_proc.live:
            err_thread = Thread(name='stderr_server',
                                target=ffmpeg_stderr_reader,
                                args=(ff_proc.live.stderr, '[Server]'))
            err_thread.daemon = True
            err_thread.start()

            while True:
                buffer = ff_proc.live.stdout.read(COPY_BUFSIZE)
                if not buffer:
                    break

                que.put(buffer)

        sleep(.33)


def check_time(node, get_source):
    current_time = get_time('full_sec')
    clip_length = node['out'] - node['seek']
    clip_end = current_time + clip_length

    if playlist.mode and not stdin_args.folder and clip_end > current_time:
        get_source.first = True


def output():
    """
    this output is for streaming to a target address,
    like rtmp, rtp, svt, etc.
    """
    year = get_date(False).split('-')[0]
    overlay = []
    node = None
    dec_cmd = []
    live_on = False
    streaming_queue = Queue(maxsize=0)

    ff_pre_settings = [
        '-pix_fmt', 'yuv420p', '-r', str(pre.fps),
        '-c:v', 'mpeg2video', '-g', '1',
        '-b:v', f'{pre.v_bitrate}k',
        '-minrate', f'{pre.v_bitrate}k',
        '-maxrate', f'{pre.v_bitrate}k',
        '-bufsize', f'{pre.v_bufsize}k'
        ] + pre_audio_codec() + ['-f', 'mpegts', '-']

    if lower_third.add_text and not lower_third.over_pre:
        messenger.info(
            f'Using drawtext node, listening on address: {lower_third.address}'
            )
        overlay = [
            '-vf',
            "null,zmq=b=tcp\\\\://'{}',drawtext=text='':fontfile='{}'".format(
                lower_third.address.replace(':', '\\:'), lower_third.fontfile)
        ]

    rtmp_server_thread = Thread(name='ffmpeg_server',target=rtmp_server,
                                args=(streaming_queue, ff_pre_settings))
    rtmp_server_thread.daemon = True
    rtmp_server_thread.start()

    try:
        enc_cmd = [
            'ffmpeg', '-v', f'level+{log.ff_level.lower()}', '-hide_banner',
            '-nostats', '-re', '-thread_queue_size', '160', '-i', 'pipe:0'
            ] + overlay + [
                '-metadata', f'service_name={playout.name}',
                '-metadata', f'service_provider={playout.provider}',
                '-metadata', f'year={year}'
            ] + playout.ffmpeg_param + playout.stream_output

        messenger.debug(f'Encoder CMD: "{" ".join(enc_cmd)}"')

        ff_proc.encoder = Popen(enc_cmd, stdin=PIPE, stderr=PIPE)

        enc_err_thread = Thread(name='stderr_encoder',
                                target=ffmpeg_stderr_reader,
                                args=(ff_proc.encoder.stderr, '[Encoder]'))
        enc_err_thread.daemon = True
        enc_err_thread.start()

        if playlist.mode and not stdin_args.folder:
            watcher = None
            get_source = GetSourceFromPlaylist()
        else:
            messenger.info('Start folder mode')
            media = MediaStore()
            watcher = MediaWatcher(media)
            get_source = GetSourceFromFolder(media)

        try:
            for node in get_source.next():
                if watcher is not None:
                    watcher.current_clip = node.get('source')

                messenger.info(f'Play: {node.get("source")}')

                dec_cmd = [
                    'ffmpeg', '-v', f'level+{log.ff_level.lower()}',
                    '-hide_banner', '-nostats'
                    ] + node['src_cmd'] + node['filter'] + ff_pre_settings

                messenger.debug(f'Decoder CMD: "{" ".join(dec_cmd)}"')

                with Popen(
                        dec_cmd, stdout=PIPE, stderr=PIPE) as ff_proc.decoder:
                    dec_err_thread = Thread(name='stderr_decoder',
                                            target=ffmpeg_stderr_reader,
                                            args=(ff_proc.decoder.stderr,
                                                  '[Decoder]'))
                    dec_err_thread.daemon = True
                    dec_err_thread.start()

                    while True:
                        buf_dec = ff_proc.decoder.stdout.read(COPY_BUFSIZE)
                        if not streaming_queue.empty():
                            buf_live = streaming_queue.get()
                            ff_proc.encoder.stdin.write(buf_live)
                            live_on = True

                            del buf_dec
                        elif buf_dec:
                            ff_proc.encoder.stdin.write(buf_dec)
                        else:
                            if live_on:
                                check_time(node, get_source)
                                live_on = False
                            break

        except BrokenPipeError as err:
            messenger.error('Broken Pipe!')
            messenger.debug(79 * '-')
            messenger.debug(f'error: "{err}"')
            messenger.debug(f'delta: "{sync_op.time_delta}"')
            messenger.debug(f'node: "{node}"')
            messenger.debug(f'dec_cmd: "{dec_cmd}"')
            messenger.debug(79 * '-')
            terminate_processes(watcher)

        except SystemExit:
            messenger.info('Got close command')
            terminate_processes(watcher)

            if ff_proc.live and ff_proc.live.poll() is None:
                ff_proc.live.terminate()

        except KeyboardInterrupt:
            messenger.warning('Program terminated')
            terminate_processes(watcher)

            if ff_proc.live and ff_proc.live.poll() is None:
                ff_proc.live.terminate()

        # close encoder when nothing is to do anymore
        if ff_proc.encoder.poll() is None:
            ff_proc.encoder.kill()

            if ff_proc.live and ff_proc.live.poll() is None:
                ff_proc.live.kill()

    finally:
        if ff_proc.encoder.poll() is None:
            ff_proc.encoder.kill()
        ff_proc.encoder.wait()

        if ff_proc.live and ff_proc.live.poll() is None:
                ff_proc.live.kill()
