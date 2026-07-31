# ASR Audio Decoding

> 14 nodes

## Key Concepts

- **asr_client.py** (11 connections) — `server/asr_client.py`
- **ASRError** (7 connections) — `server/asr_client.py`
- **transcribe_wav_bytes()** (5 connections) — `server/asr_client.py`
- **_decode_with_pyav()** (4 connections) — `server/asr_client.py`
- **decode_to_wav16k()** (4 connections) — `server/asr_client.py`
- **_next_base()** (4 connections) — `server/asr_client.py`
- **_split_urls()** (2 connections) — `server/asr_client.py`
- **_post_once()** (2 connections) — `server/asr_client.py`
- **RuntimeError** (1 connections)
- **ASR 客户端：把上传音频解码成 16k 单声道 WAV，再发给 HY ContextASR 服务转写。  协议：OpenAI 多模态 Chat Complet** (1 connections) — `server/asr_client.py`
- **解码或转写失败，消息对用户友好（前端直接展示）。** (1 connections) — `server/asr_client.py`
- **用 PyAV 解码任意容器（webm/mp4/ogg 等），返回 (float32 ndarray, sample_rate)。     PyAV 内置 ffm** (1 connections) — `server/asr_client.py`
- **把上传的音频字节解码成 16k 单声道 PCM16 WAV bytes。      先用 soundfile 解（快，支持 ogg/wav/mp3/flac），** (1 connections) — `server/asr_client.py`
- **对 base64 的 WAV 调 ASR，返回识别文本。失败抛 ASRError。      多端点 round-robin；可重试错误（5xx/网络）退避重试** (1 connections) — `server/asr_client.py`

## Relationships

- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (2 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (1 shared connections)

## Source Files

- `server/asr_client.py`

## Audit Trail

- EXTRACTED: 45 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*