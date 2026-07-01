"""ASR 客户端：把上传音频解码成 16k 单声道 WAV，再发给 HY ContextASR 服务转写。

协议：OpenAI 多模态 Chat Completions（POST {base}/v1/chat/completions），
  messages[].content=[{"type":"audio_url","audio_url":{"url":"data:audio/wav;base64,..."}}]
  返回 choices[0].message.content。

实现裁剪自 scripts/sft_clean/asr_client.py，去掉缓存/patch/aiohttp，只保留浏览器录音转写
这条链路所需：soundfile 解码（支持 ogg/opus、wav、mp3、flac；不支持 webm 容器、mp4/aac）
+ librosa 重采样 + urllib POST + 多端点 round-robin + 退避重试。不引第三方 HTTP 库。
"""
import io
import json
import threading
import time
import urllib.error
import urllib.request

from . import config

SAMPLE_RATE = 16000


class ASRError(RuntimeError):
    """解码或转写失败，消息对用户友好（前端直接展示）。"""


# ---------------- 解码 → 16k 单声道 PCM16 WAV ----------------
def _decode_with_pyav(raw: bytes) -> tuple:
    """用 PyAV 解码任意容器（webm/mp4/ogg 等），返回 (float32 ndarray, sample_rate)。
    PyAV 内置 ffmpeg 绑定，不需要系统 ffmpeg。"""
    import av
    import numpy as np

    frames = []
    sr_out = None
    container = av.open(io.BytesIO(raw))
    for frame in container.decode(audio=0):
        if sr_out is None:
            sr_out = frame.sample_rate
        arr = frame.to_ndarray()          # shape: (channels, samples) or (samples,)
        mono = arr.mean(axis=0) if arr.ndim > 1 else arr
        frames.append(mono.astype(np.float32))
    if not frames:
        raise ASRError("音频内容为空")
    return np.concatenate(frames), int(sr_out or SAMPLE_RATE)


def decode_to_wav16k(raw: bytes, hint: str = "") -> bytes:
    """把上传的音频字节解码成 16k 单声道 PCM16 WAV bytes。

    先用 soundfile 解（快，支持 ogg/wav/mp3/flac），
    失败则回退 PyAV（内置 ffmpeg，支持 webm/mp4/aac 等浏览器格式）。
    """
    import numpy as np
    import soundfile as sf

    if not raw:
        raise ASRError("录音为空")

    y = sr = None
    try:
        y, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=False)
        if getattr(y, "ndim", 1) > 1:
            y = y.mean(axis=1)
        sr = int(sr)
    except Exception:  # noqa: BLE001
        pass

    if y is None:
        # soundfile 解不了（常见：Chrome 录的 webm/opus，iOS 录的 mp4/aac）→ 用 PyAV
        try:
            y, sr = _decode_with_pyav(raw)
        except ASRError:
            raise
        except Exception as e:  # noqa: BLE001
            raise ASRError(f"无法解码录音（{hint or 'unknown'}）：{e!r}") from e

    if sr != SAMPLE_RATE:
        import librosa
        y = librosa.resample(y, orig_sr=sr, target_sr=SAMPLE_RATE)

    pcm = (np.clip(y, -1.0, 1.0) * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    sf.write(buf, pcm, SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return buf.getvalue()


# ---------------- 转写（多端点 round-robin + 退避重试）----------------
_rr_lock = threading.Lock()
_rr = 0


def _split_urls(s: str) -> list[str]:
    out = []
    for part in (s or "").replace(";", ",").split(","):
        part = part.strip().rstrip("/")
        if part:
            out.append(part)
    return out


def _next_base() -> str:
    global _rr
    bases = _split_urls(config.ASR2_BASE_URLS)
    if not bases:
        raise ASRError("ASR 服务未配置（ASR2_BASE_URLS 为空）")
    with _rr_lock:
        b = bases[_rr % len(bases)]
        _rr += 1
    return b


def _post_once(base: str, audio_url: str) -> str:
    url = f"{base}/v1/chat/completions"
    body = json.dumps({
        "model": config.ASR2_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "audio_url", "audio_url": {"url": audio_url}},
        ]}],
        "max_tokens": 2048,
        "temperature": 0.0,
    }, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if config.ASR2_API_KEY:
        headers["Authorization"] = f"Bearer {config.ASR2_API_KEY}"
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=config.ASR2_TIMEOUT) as r:
        raw = r.read().decode("utf-8", "replace")
    return json.loads(raw)["choices"][0]["message"]["content"].strip()


def transcribe_wav_bytes(wav_b64: str, retries: int = 2) -> str:
    """对 base64 的 WAV 调 ASR，返回识别文本。失败抛 ASRError。

    多端点 round-robin；可重试错误（5xx/网络）退避重试，4xx 客户端错误不重试。
    """
    audio_url = f"data:audio/wav;base64,{wav_b64}"
    backoffs = [0.0] + [1.0 * (2 ** i) for i in range(retries)]
    last = ""
    for i, backoff in enumerate(backoffs):
        if backoff:
            time.sleep(backoff)
        base = _next_base()
        try:
            text = _post_once(base, audio_url)
            return text
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:160]
            except Exception:  # noqa: BLE001
                pass
            if e.code in (400, 422):     # 客户端错误，换端点重试无意义
                raise ASRError(f"ASR 拒绝该音频（HTTP {e.code}）：{detail}") from e
            last = f"HTTP {e.code} {detail}"
        except (urllib.error.URLError, OSError, KeyError, IndexError, ValueError) as e:
            last = repr(e)[:160]
    raise ASRError(f"ASR 请求失败（已重试 {retries} 次）：{last}")
