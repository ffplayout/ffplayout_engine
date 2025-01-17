use std::{process::Stdio, sync::atomic::Ordering};

use async_iterator::Iterator;
use log::*;
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt, BufReader, BufWriter},
    process::Command,
    time::{sleep, Duration},
};

mod desktop;
mod hls;
mod null;
mod stream;

pub use hls::write_hls;

use crate::player::{
    controller::{ChannelManager, ProcessUnit::*},
    input::{ingest_server, source_generator},
    utils::{sec_to_time, stderr_reader},
};
use crate::utils::{
    config::OutputMode::*,
    errors::ServiceError,
    logging::{fmt_cmd, Target},
    task_runner,
};
use crate::vec_strings;

/// Player
///
/// Here we create the input file loop, from playlist, or folder source.
/// Then we read the stdout from the reader ffmpeg instance
/// and write it to the stdin from the streamer ffmpeg instance.
/// If it is configured we also fire up a ffmpeg ingest server instance,
/// for getting live feeds.
/// When a live ingest arrive, it stops the current playing and switch to the live source.
/// When ingest stops, it switch back to playlist/folder mode.
pub async fn player(manager: ChannelManager) -> Result<(), ServiceError> {
    let config = manager.config.lock().await.clone();
    let id = config.general.channel_id;
    let config_clone = config.clone();
    let ff_log_format = format!("level+{}", config.logging.ffmpeg_level.to_lowercase());
    let ignore_enc = config.logging.ignore_lines.clone();
    let playlist_init = manager.list_init.clone();
    let is_alive = manager.is_alive.clone();
    let ingest_is_alive = manager.ingest_is_alive.clone();
    let mut buffer = vec![0u8; 64 * 1024];
    let mut live_on = false;

    // get source iterator
    let mut node_sources = source_generator(manager.clone()).await;

    // get ffmpeg output instance
    let mut enc_proc = match config.output.mode {
        Desktop => desktop::output(&config, &ff_log_format).await?,
        Null => null::output(&config, &ff_log_format).await?,
        Stream => stream::output(&config, &ff_log_format).await?,
        _ => panic!("Output mode doesn't exists!"),
    };

    let mut enc_writer = BufWriter::new(enc_proc.stdin.take().unwrap());
    let enc_err = BufReader::new(enc_proc.stderr.take().unwrap());

    *manager.encoder.lock().await = Some(enc_proc);
    let enc_p_ctl = manager.clone();

    // spawn a task to log ffmpeg output error messages
    let error_encoder_task = tokio::spawn(stderr_reader(enc_err, ignore_enc, Encoder, enc_p_ctl));

    let channel_mgr_2 = manager.clone();

    // spawn a task for ffmpeg ingest server and create a channel for package sending
    let ingest_srv = if config.ingest.enable {
        Some(tokio::spawn(ingest_server(config_clone, channel_mgr_2)))
    } else {
        None
    };

    while let Some(node) = node_sources.next().await {
        *manager.current_media.lock().await = Some(node.clone());
        let ignore_dec = config.logging.ignore_lines.clone();

        if !is_alive.load(Ordering::SeqCst) {
            debug!(target: Target::file_mail(), channel = id; "Playout is stopped, break out from source loop");
            break;
        }

        trace!("Decoder CMD: {:?}", node.cmd);

        let mut cmd = match node.cmd {
            Some(cmd) => cmd,
            None => break,
        };

        if node.skip {
            // skip is different from node.cmd = None.
            // This source is valid, but too short to play,
            // so better skip it and go to the next one.
            continue;
        }

        let c_index = if cfg!(debug_assertions) {
            format!(
                " ({}/{})",
                node.index.unwrap() + 1,
                manager.current_list.lock().await.len()
            )
        } else {
            String::new()
        };

        info!(target: Target::file_mail(), channel = id;
            "Play for <yellow>{}</>{c_index}: <b><magenta>{}  {}</></b>",
            sec_to_time(node.out - node.seek),
            node.source,
            node.audio
        );

        if config.task.enable {
            if config.task.path.is_file() {
                let channel_mgr_3 = manager.clone();

                tokio::spawn(task_runner::run(channel_mgr_3));
            } else {
                error!(target: Target::file_mail(), channel = id;
                    "<bright-blue>{:?}</> executable not exists!",
                    config.task.path
                );
            }
        }

        let mut dec_cmd = vec_strings!["-hide_banner", "-nostats", "-v", &ff_log_format];

        if let Some(decoder_input_cmd) = &config.advanced.decoder.input_cmd {
            dec_cmd.append(&mut decoder_input_cmd.clone());
        }

        dec_cmd.append(&mut cmd);

        if let Some(mut filter) = node.filter {
            dec_cmd.append(&mut filter.cmd());
            dec_cmd.append(&mut filter.map());
        }

        if config.processing.vtt_enable && dec_cmd.iter().any(|s| s.ends_with(".vtt")) {
            let i = dec_cmd
                .iter()
                .filter(|&n| n == "-i")
                .count()
                .saturating_sub(1);

            dec_cmd.append(&mut vec_strings!("-map", format!("{i}:s"), "-c:s", "copy"));
        }

        if let Some(cmd) = &config.processing.cmd {
            dec_cmd.extend_from_slice(cmd);
        }

        debug!(target: Target::file_mail(), channel = id;
            "Decoder CMD: <bright-blue>ffmpeg {}</>",
            fmt_cmd(&dec_cmd)
        );

        // create ffmpeg decoder instance, for reading the input files
        let mut dec_proc = Command::new("ffmpeg")
            .args(dec_cmd)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()?;

        let mut decoder_stdout = dec_proc.stdout.take().unwrap();
        let dec_err = BufReader::new(dec_proc.stderr.take().unwrap());

        *manager.clone().decoder.lock().await = Some(dec_proc);
        let channel_mgr_c = manager.clone();

        let error_decoder_task =
            tokio::spawn(stderr_reader(dec_err, ignore_dec, Decoder, channel_mgr_c));

        loop {
            if ingest_is_alive.load(Ordering::SeqCst) {
                // read from ingest server instance
                if !live_on {
                    info!(target: Target::file_mail(), channel = id; "Switch from {} to live ingest", config.processing.mode);

                    manager.stop(Decoder).await?;
                    live_on = true;
                    playlist_init.store(true, Ordering::SeqCst);
                }

                let mut ingest_stdout_guard = manager.ingest_stdout.lock().await;
                if let Some(ref mut ingest_stdout) = *ingest_stdout_guard {
                    let num = ingest_stdout.read(&mut buffer[..]).await?;

                    if num == 0 {
                        break;
                    }

                    enc_writer.write_all(&buffer[..num]).await?;
                }
            } else {
                // read from decoder instance
                if live_on {
                    info!(target: Target::file_mail(), channel = id; "Switch from live ingest to {}", config.processing.mode);

                    live_on = false;
                    break;
                }

                let num = decoder_stdout.read(&mut buffer[..]).await?;

                if num == 0 {
                    break;
                }

                enc_writer.write_all(&buffer[..num]).await?;
            }
        }

        drop(decoder_stdout);

        manager.wait(Decoder).await?;
        error_decoder_task.await??;
    }

    trace!("Out of source loop");

    sleep(Duration::from_secs(1)).await;

    if let Some(ingest) = ingest_srv {
        ingest.await??;
    }

    manager.stop_all(false).await?;
    error_encoder_task.await??;

    Ok(())
}
