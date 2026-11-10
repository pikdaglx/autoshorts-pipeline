import os
import re
import json
import time
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from google import genai
from google.genai import types
from youtube_transcript_api import YouTubeTranscriptApi
import yt_dlp

from src.config import BASE_DIR, OUTPUT_DIR, TEMP_DIR, GEMINI_API_KEY, get_ffmpeg_path
from src.google_services import YouTubeService, get_credentials
from src.quality_auditor import ClipQualityAuditor

YT_ID_REGEX = re.compile(
    r"(?:v=|\/|youtu\.be\/|shorts\/|embed\/)([0-9A-Za-z_-]{11})(?:\?|&|$)"
)

def extract_video_id(url_or_id: str) -> str:
    """Extrai o ID de 11 caracteres de qualquer URL do YouTube."""
    if len(url_or_id) == 11 and re.match(r"^[0-9A-Za-z_-]{11}$", url_or_id):
        return url_or_id
    match = YT_ID_REGEX.search(url_or_id)
    if match:
        return match.group(1)
    raise ValueError(f"Nao foi possivel identificar o ID do video a partir de: {url_or_id}")


def get_video_metadata(url: str) -> Dict[str, Any]:
    """Obtém metadados básicos do vídeo (título, canal, duração), com fallback oEmbed à prova de falhas."""
    vid_id = extract_video_id(url)
    cookie_file = os.environ.get("YOUTUBE_COOKIES_FILE", "cookies.txt")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "mweb"]}},
    }
    if os.path.exists(cookie_file):
        ydl_opts["cookiefile"] = cookie_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "id": info.get("id", vid_id),
                "title": info.get("title", "Podcast"),
                "channel": info.get("uploader", info.get("channel", "Canal")),
                "duration": info.get("duration", 0),
                "webpage_url": info.get("webpage_url", url),
            }
    except Exception as e:
        print(f"[!] yt-dlp extract_info bloqueado ({e}). Usando fallback oficial oEmbed...", flush=True)
        try:
            import urllib.request
            oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={vid_id}&format=json"
            req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return {
                    "id": vid_id,
                    "title": data.get("title", "Podcast"),
                    "channel": data.get("author_name", "Canal"),
                    "duration": 0,
                    "webpage_url": url,
                }
        except Exception as oe:
            print(f"[!] Fallback oEmbed falhou: {oe}", flush=True)
            return {
                "id": vid_id,
                "title": f"Episódio {vid_id}",
                "channel": "Podcast",
                "duration": 0,
                "webpage_url": url,
            }


def get_transcript(video_id: str) -> List[Dict[str, Any]]:
    """Obtém transcrição timestampada do YouTube (com cache em disco para evitar rate limits)."""
    cache_path = TEMP_DIR / f"transcript_{video_id}.json"
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cached = json.load(f)
                if cached:
                    return cached
        except Exception:
            pass

    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
        transcript = None
        for lang_code in ["pt", "pt-BR", "en"]:
            try:
                transcript = transcript_list.find_transcript([lang_code])
                if transcript:
                    break
            except Exception:
                continue

        if not transcript:
            for t in transcript_list:
                transcript = t
                break

        if transcript:
            data = transcript.fetch()
            clean_entries = []
            for item in data:
                start = float(item["start"] if isinstance(item, dict) else getattr(item, "start", 0))
                duration = float(item["duration"] if isinstance(item, dict) else getattr(item, "duration", 0))
                text = str(item["text"] if isinstance(item, dict) else getattr(item, "text", "")).strip().replace("\n", " ")
                clean_entries.append({
                    "start": start,
                    "duration": duration,
                    "end": round(start + duration, 2),
                    "text": text,
                })
            if clean_entries:
                try:
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(clean_entries, f, ensure_ascii=False)
                except Exception:
                    pass
            return clean_entries
    except Exception as e:
        print(f"[!] Aviso ao obter transcricao: {e}", flush=True)

    return []


def format_transcript_for_prompt(entries: List[Dict[str, Any]], max_chars: int = 150000) -> str:
    """Formata a transcrição em blocos de tempo para a IA."""
    lines = []
    current_chars = 0
    for e in entries:
        m, s = divmod(int(e["start"]), 60)
        h, m = divmod(m, 60)
        timecode = f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"
        line = f"[{timecode}] ({e['start']:.1f}s - {e['end']:.1f}s): {e['text']}"
        lines.append(line)
        current_chars += len(line)
        if current_chars >= max_chars:
            lines.append("... [transcricao truncada]")
            break
    return "\n".join(lines)


def find_viral_moments_with_gemini(
    transcript_text: str,
    video_title: str,
    channel_name: str,
    max_clips: int = 1,
) -> List[Dict[str, Any]]:
    """Minera cortes virais usando o novo SDK Gemini (gemini-2.0-flash) e JSON Schema."""
    if not GEMINI_API_KEY:
        print("[!] Chave GEMINI_API_KEY não definida.", flush=True)
        return []

    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""Você é o maior estrategista de YouTube Shorts e engenheiro de viralização do mundo.
Sua missão é extrair cortes com potencial de MILHÕES de visualizações do podcast "{video_title}" ({channel_name}), aplicando rigorosamente as SKILLS OFICIAIS DE CRESCIMENTO NO YOUTUBE (yt-shorts, yt-viral, yt-package, yt-script, yt-seo, yt-retention).

METODOLOGIA OBRIGATÓRIA DAS SKILLS:

1. FILTRAGEM DE MOMENTO (yt-shorts):
- O corte DEVE sobreviver sozinho sem o resto do vídeo (100% autônomo).
- Deve começar em um pensamento NOVO e completo (NUNCA no meio de uma resposta sem contexto).
- DURAÇÃO ÓTIMA: Entre 35 e 55 segundos (NUNCA abaixo de 25s, NUNCA acima de 58s).
- DEVE TER O "GIRO" (TURN): Uma afirmação forte inicial, seguida de uma quebra de expectativa, segredo revelado, comprovação ou lição chocante.
- FINAL LIMPO E LOOP: Termina no clímax ou numa frase de impacto reflexiva. Idealmente, a última frase conecta naturalmente de volta com o gancho inicial.

2. ENGENHARIA DE GANCHO VIRAL (yt-script & yt-viral):
O início do corte deve se encaixar em uma das fórmulas virais de alto engajamento:
- A Estatística Chocante ("90% das pessoas não sabem disso...")
- O Erro Comum ("O maior erro que destrói a sua carreira...")
- Quebra de Senso Comum / Contrarian Flip ("Todo mundo acha que X dá dinheiro, mas na verdade...")
- Resultado de Terceiro ("Como ele saiu do zero a 10 milhões...")
- Revelação / Segredo Íntimo ("O dia que eu quase perdi tudo...")

3. PACKAGING DE ALTA CTR (yt-package):
- TÍTULO: Máximo de 55 caracteres (evita corte em mobile). Deve gerar curiosidade irresistível sem clickbait falso. Use números, nomes específicos ou verbos fortes. No máximo 2 palavras em CAIXA ALTA.
- TEXTO DE TELA (hook_text): 3 a 5 palavras de altíssimo impacto para prender a atenção nos primeiros 2 segundos, com palavras DIFERENTES do que está sendo falado.

4. SEO & ALGORITMO (yt-seo):
- DESCRIÇÃO: 2 linhas de gancho intrigante para busca orgânica + créditos claros ao podcast original ({channel_name}) + hashtags de alta tração (#Shorts #Cortes #Podcast).
- TAGS: 6 a 8 tags cirúrgicas combinando o nicho, o tema específico, o canal de origem e termos de alta busca.

TRANSCRIÇÃO TIMESTAMPADA:
{transcript_text}

Selecione os {max_clips} MELHORES cortes que maximizem retenção e probabilidade de viralizar no YouTube Shorts. Retorne estritamente o JSON no schema configurado.
"""

    json_schema = {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "required": ["start_seconds", "end_seconds", "title", "description", "tags"],
            "properties": {
                "start_seconds": {"type": "NUMBER"},
                "end_seconds": {"type": "NUMBER"},
                "title": {"type": "STRING"},
                "hook_text": {"type": "STRING"},
                "description": {"type": "STRING"},
                "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
                "viral_reason": {"type": "STRING"},
            },
        },
    }

    for attempt in range(2):
        try:
            response = client.models.generate_content(
                model="gemini-2.0-flash",
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=json_schema,
                ),
            )
            raw_text = response.text.strip() if response.text else ""
            if not raw_text:
                continue

            clips = json.loads(raw_text)
            if isinstance(clips, dict):
                clips = [clips]

            valid_clips = []
            for c in clips:
                if not isinstance(c, dict):
                    continue
                start = float(c.get("start_seconds", 0))
                end = float(c.get("end_seconds", 0))
                dur = end - start
                if 10.0 <= dur <= 60.0 and start >= 0:
                    valid_clips.append(c)

            if valid_clips:
                return valid_clips[:max_clips]

        except Exception as e:
            print(f"[!] Erro na tentativa {attempt + 1} com Gemini: {e}", flush=True)

    return []


def find_viral_moments_heuristic(
    entries: List[Dict[str, Any]],
    video_title: str,
    channel_name: str,
    max_clips: int = 1,
) -> List[Dict[str, Any]]:
    """Heurística avançada de retenção e momentos virais (polêmica, choque, segredos, valores)."""
    if not entries:
        return []

    triggers = {
        "polemica": (
            [
                "covarde", "mentira", "traição", "traiu", "inimigo", "processo", "roubo", "cadeia",
                "falsidade", "hipocrisia", "nojento", "ridículo", "escroto", "crime", "idiota",
                "merda", "culpa", "perigoso", "odeio", "destruiu", "acabou", "briga", "farsa",
                "corrupção", "roubou", "processar", "safadeza",
            ],
            12.0,
            "🚨 ELE FALOU O QUE NINGUÉM TEM CORAGEM",
            "A Verdade Sem Filtro",
        ),
        "segredo": (
            [
                "ninguém sabe", "nunca contei", "revelou", "segredo", "bastidores", "a verdade é",
                "descobri que", "abriu o jogo", "confesso", "ninguém te fala", "esconderam",
                "não podia falar", "por trás das câmeras", "escondido", "segredo guardado",
            ],
            10.0,
            "🤫 O QUE NINGUÉM NUNCA TE CONTOU",
            "O Segredo Revelado",
        ),
        "valores": (
            [
                "milhões", "milhão", "bilhão", "bilhões", "mil reais", "faturou", "perdeu tudo",
                "falência", "riqueza", "pobre", "dívida", "lucro", "prejuízo", "patrimônio",
                "faturamento", "dinheiro", "investiu", "riqueza",
            ],
            9.0,
            "💸 O SEGREDO DOS BILHÕES",
            "O Segredo de Faturar Milhões",
        ),
        "choque": (
            [
                "morreu", "morte", "quase morri", "hospital", "coma", "cirurgia", "sangue",
                "armado", "medo", "pesadelo", "socorro", "surreal", "inacreditável", "trauma",
                "chocante", "acidente", "grave", "uti", "ferro", "sobreviver", "tragédia",
            ],
            9.0,
            "⚠️ O RELATO QUE DEIXOU TODOS EM CHOQUE",
            "Sobrevivi Por Um Milagre",
        ),
        "erro": (
            [
                "maior erro", "pior erro", "nunca faça", "não faça isso", "perda de tempo",
                "ilusão", "mentiram pra você", "cuidado com", "armadilha", "destrói",
                "erro mais caro", "me arrependo", "cuidado",
            ],
            8.0,
            "🚨 O MAIOR ERRO DA VIDA DELE",
            "O Maior Erro Que Alguém Pode Cometer",
        ),
        "emocao": (
            [
                "chorei", "choro", "humilhado", "humilhação", "sozinho", "desespero",
                "depressão", "fundo do poço", "rejeitado", "solidão", "amigos verdadeiros",
                "família", "separação",
            ],
            7.0,
            "💔 A DURA REALIDADE DA VIDA",
            "A Lição Que Ninguém Esquece",
        ),
    }

    weak_openers = [
        "aí tipo", "então", "pois é", "como eu tava", "sabe né",
        "a gente tava", "na verdade assim", "tipo assim", "aí eu falei tipo",
    ]

    dangling_words = {
        "que", "no", "na", "de", "do", "da", "em", "para", "pra", "com",
        "por", "e", "ou", "se", "o", "a", "os", "as", "um", "uma", "você",
        "ainda", "muito", "mais", "já", "mas", "quando", "como", "sem",
        "sobre", "entre", "até", "nem"
    }

    total_duration = entries[-1]["end"] if entries else 0.0
    scored_windows = []
    for i in range(len(entries)):
        start_time = entries[i]["start"]
        # Filtro Anti-Intro: Ignora os primeiros 45 segundos em vídeos longos (> 3 min)
        if total_duration > 180.0 and start_time < 45.0:
            continue

        curr_text = []

        first_phrase = entries[i]["text"].lower().strip()
        has_weak_opener = any(first_phrase.startswith(w) for w in weak_openers)

        for j in range(i, len(entries)):
            e = entries[j]
            curr_text.append(e["text"])
            dur = e["end"] - start_time

            if 25.0 <= dur <= 58.0:
                full_txt = " ".join(curr_text).lower()
                score = 0.0
                cat_scores = {}

                for cat, (words, weight, headline, default_title) in triggers.items():
                    hits = sum(1 for w in words if w in full_txt)
                    if hits > 0:
                        cat_pts = hits * weight
                        score += cat_pts
                        cat_scores[cat] = cat_pts

                # Bônus para gancho com pergunta direta no início
                if "?" in entries[i]["text"]:
                    score += 8.0
                elif "?" in full_txt:
                    score += 3.0

                # Densidade de palavras (evita trechos de silêncio vazio)
                word_count = len(full_txt.split())
                score += min(10.0, word_count / 12.0)

                # Penalidade severa para aberturas fracas/preenchimento
                if has_weak_opener:
                    score -= 15.0

                top_cat = max(cat_scores.items(), key=lambda x: x[1])[0] if cat_scores else "erro"
                headline = triggers[top_cat][2]
                base_title = triggers[top_cat][3]

                scored_windows.append({
                    "start_seconds": start_time,
                    "end_seconds": e["end"],
                    "score": score,
                    "duration": dur,
                    "top_cat": top_cat,
                    "headline": headline,
                    "base_title": base_title,
                    "first_line": entries[i]["text"].strip(),
                })
            elif dur > 58.0:
                break

    scored_windows.sort(key=lambda x: x["score"], reverse=True)

    # Filtrar seleções não-sobrepostas e aprovadas pelo Quality Auditor
    selected = []
    for cand in scored_windows:
        overlap = False
        for s in selected:
            if not (cand["end_seconds"] <= s["start_seconds"] or cand["start_seconds"] >= s["end_seconds"]):
                overlap = True
                break
        if overlap:
            continue

        # Validar narrativa, anti-intro e frase completa
        window_text = " ".join(
            e["text"] for e in entries if cand["start_seconds"] <= e["start"] <= cand["end_seconds"]
        )
        qa = ClipQualityAuditor.audit_clip(
            window_text,
            cand["start_seconds"],
            cand["end_seconds"],
            is_full_episode=(total_duration > 180.0)
        )
        if not qa["approved"]:
            continue

        selected.append(cand)
        if len(selected) >= max_clips:
            break

    clips = []
    for item in selected:
        first_clean = item["first_line"].replace("?", "").replace(".", "").strip()
        words = first_clean.split()

        title_words = []
        for w in words:
            title_words.append(w)
            candidate = " ".join(title_words)
            if len(candidate) >= 32:
                break

        while title_words and title_words[-1].lower() in dangling_words:
            title_words.pop()

        title_phrase = " ".join(title_words)
        if len(title_phrase) < 10:
            title_phrase = item["base_title"]

        final_title = f"{title_phrase} | {channel_name}"
        if len(final_title) > 55:
            final_title = f"{title_phrase[:50 - len(channel_name)]}... | {channel_name}"

        clips.append({
            "start_seconds": item["start_seconds"],
            "end_seconds": item["end_seconds"],
            "title": final_title,
            "hook_text": item["headline"],
            "description": f"Trecho imperdível de {video_title} ({channel_name}).\n\nCréditos ao canal original: {channel_name}\n\n#Shorts #Cortes #Podcast",
            "tags": ["shorts", "cortes", "podcast", item["top_cat"], channel_name.lower()[:20]],
            "viral_reason": f"Momento com gatilho de {item['top_cat']} (Score {item['score']:.1f}) e gancho de alta retenção.",
        })

    return clips


def download_clip_segment(
    url: str,
    start_sec: float,
    end_sec: float,
    output_raw_path: str,
) -> bool:
    """Baixa apenas a seção necessária usando yt-dlp."""
    ffmpeg_exe = get_ffmpeg_path()

    def to_time_str(secs: float) -> str:
        m, s = divmod(int(secs), 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    start_str = to_time_str(start_sec)
    end_str = to_time_str(end_sec)
    print(f"[⬇] Baixando corte de {start_str} até {end_str}...", flush=True)

    ydl_opts = {
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "outtmpl": output_raw_path,
        "download_ranges": yt_dlp.utils.download_range_func(None, [(start_sec, end_sec)]),
        "force_keyframes_at_cuts": True,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": ffmpeg_exe,
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "mweb"]}},
    }

    cookie_file = os.environ.get("YOUTUBE_COOKIES_FILE", "cookies.txt")
    if os.path.exists(cookie_file):
        ydl_opts["cookiefile"] = cookie_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return os.path.exists(output_raw_path) and os.path.getsize(output_raw_path) > 10000
    except Exception as e:
        print(f"[!] Erro no download do trecho: {e}", flush=True)
        return False


def build_ass_subtitles(
    entries: List[Dict[str, Any]],
    clip_start: float,
    clip_end: float,
    output_ass_path: str,
    headline_text: Optional[str] = None,
):
    """Gera legendas .ass dinâmicas (estilo Hormozi/Shorts) com blocos rápidos e banner superior de gancho."""
    def format_ass_time(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        cs = int((s - int(s)) * 100)
        return f"{h:d}:{m:02d}:{int(s):02d}.{cs:02d}"

    ass_header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ShortsStyle,Arial Black,78,&H0000FFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,1,0,1,8,3,2,60,60,480,1
Style: HeadlineStyle,Arial Black,62,&H00FFFFFF,&H000000FF,&H00000000,&H90000000,-1,0,0,0,100,100,1,0,1,10,4,8,60,60,260,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []

    if headline_text and headline_text.strip():
        # Exibe o banner de gancho no topo durante os primeiros 3.5 segundos
        banner_dur = min(3.50, max(1.5, clip_end - clip_start))
        t_head_start = format_ass_time(0.0)
        t_head_end = format_ass_time(banner_dur)
        clean_headline = headline_text.strip().upper().replace("{", "").replace("}", "")
        events.append(f"Dialogue: 1,{t_head_start},{t_head_end},HeadlineStyle,,0,0,0,,{clean_headline}")
    max_words_per_chunk = 4

    for e in entries:
        if e["end"] < clip_start or e["start"] > clip_end:
            continue

        raw_text = e["text"].strip().replace("{", "").replace("}", "")
        words = raw_text.split()
        if not words:
            continue

        entry_dur = max(0.4, e["end"] - e["start"])
        time_per_word = entry_dur / len(words)

        for i in range(0, len(words), max_words_per_chunk):
            chunk_words = words[i:i + max_words_per_chunk]
            chunk_start = e["start"] + (i * time_per_word)
            chunk_end = chunk_start + (len(chunk_words) * time_per_word)

            if chunk_end < clip_start or chunk_start > clip_end:
                continue

            rel_start = max(0.0, chunk_start - clip_start)
            rel_end = min(clip_end - clip_start, chunk_end - clip_start)

            if rel_end - rel_start < 0.2:
                continue

            t_start = format_ass_time(rel_start)
            t_end = format_ass_time(rel_end)
            clean_text = " ".join(chunk_words).upper()

            events.append(f"Dialogue: 0,{t_start},{t_end},ShortsStyle,,0,0,0,,{clean_text}")

    with open(output_ass_path, "w", encoding="utf-8") as f:
        f.write(ass_header + "\n".join(events) + "\n")


def render_short_916_with_subtitles(
    raw_video_path: str,
    ass_subtitle_path: str,
    output_path: str,
    music_path: Optional[str] = None,
) -> bool:
    """Renderiza vídeo 9:16 vertical 1080x1920 com legendas queimadas e trilha sonora de fundo."""
    ffmpeg_exe = get_ffmpeg_path()
    work_dir = os.path.dirname(os.path.abspath(output_path))
    ass_filename = os.path.basename(ass_subtitle_path)

    temp_ass = os.path.join(work_dir, ass_filename)
    if not os.path.exists(temp_ass):
        shutil.copy2(ass_subtitle_path, temp_ass)

    # Buscar trilha de fundo se disponivel
    bgm_file = music_path
    if not bgm_file:
        music_dir = BASE_DIR / "assets" / "music"
        if music_dir.exists():
            tracks = list(music_dir.glob("*.mp3"))
            if tracks:
                bgm_file = str(tracks[0])

    use_bgm = bgm_file and os.path.exists(bgm_file)

    if use_bgm:
        print(f"[🎵] Mixando trilha sonora de fundo: {os.path.basename(bgm_file)} (volume sutil a 7%)...", flush=True)
        filter_complex = (
            f"[0:v]crop=ih*(9/16):ih:(iw-ow)/2:0,scale=1080:1920,ass={ass_filename}[vout];"
            f"[0:a]volume=1.20[vocal];"
            f"[1:a]volume=0.07,aloop=loop=-1:size=2e+09[bgm];"
            f"[vocal][bgm]amix=inputs=2:duration=first:dropout_transition=2:weights=1.0 0.2[aout]"
        )
        cmd = [
            ffmpeg_exe,
            "-y",
            "-i", os.path.abspath(raw_video_path),
            "-i", os.path.abspath(bgm_file),
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", "[aout]",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "20",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            os.path.abspath(output_path),
        ]
    else:
        filter_complex = f"crop=ih*(9/16):ih:(iw-ow)/2:0,scale=1080:1920,ass={ass_filename}"
        cmd = [
            ffmpeg_exe,
            "-y",
            "-i", os.path.abspath(raw_video_path),
            "-vf", filter_complex,
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "20",
            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "44100",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            os.path.abspath(output_path),
        ]

    print(f"[🎬] Renderizando Shorts vertical 1080x1920 com legendas dinâmicas...", flush=True)
    res = subprocess.run(cmd, cwd=work_dir, capture_output=True, text=True)
    if res.returncode != 0:
        # Fallback sem libass se falhar
        if use_bgm:
            filter_simple = (
                f"[0:v]crop=ih*(9/16):ih:(iw-ow)/2:0,scale=1080:1920[vout];"
                f"[0:a]volume=1.20[vocal];"
                f"[1:a]volume=0.07,aloop=loop=-1:size=2e+09[bgm];"
                f"[vocal][bgm]amix=inputs=2:duration=first:dropout_transition=2:weights=1.0 0.2[aout]"
            )
            cmd_simple = [
                ffmpeg_exe,
                "-y",
                "-i", os.path.abspath(raw_video_path),
                "-i", os.path.abspath(bgm_file),
                "-filter_complex", filter_simple,
                "-map", "[vout]",
                "-map", "[aout]",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "20",
                output_path,
            ]
        else:
            cmd_simple = [
                ffmpeg_exe,
                "-y",
                "-i", os.path.abspath(raw_video_path),
                "-vf", "crop=ih*(9/16):ih:(iw-ow)/2:0,scale=1080:1920",
                "-c:v", "libx264",
                "-preset", "fast",
                "-crf", "20",
                "-c:a", "aac",
                "-b:a", "192k",
                output_path,
            ]
        res2 = subprocess.run(cmd_simple, cwd=work_dir, capture_output=True, text=True)
        return res2.returncode == 0

    return os.path.exists(output_path) and os.path.getsize(output_path) > 10000


class AutoPodcastCutter:
    """Pipeline principal de cortes para YouTube Shorts."""

    def __init__(self):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        TEMP_DIR.mkdir(parents=True, exist_ok=True)

    def process_url(
        self,
        youtube_url: str,
        upload_to_youtube: bool = True,
        privacy: str = "private",
        publish_at: Optional[datetime] = None,
        max_clips: int = 1,
        custom_start: Optional[float] = None,
        custom_end: Optional[float] = None,
        custom_title: Optional[str] = None,
        custom_hook: Optional[str] = None,
        music_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        video_id = extract_video_id(youtube_url)
        print(f"\n[🚀] Iniciando AutoCortes para o vídeo ID: {video_id}", flush=True)

        meta = get_video_metadata(youtube_url)
        print(f"[📺] Vídeo: {meta['title']} | Canal: {meta['channel']}", flush=True)

        entries = get_transcript(video_id)
        if not entries and (custom_start is None or custom_end is None):
            raise RuntimeError(f"O vídeo {video_id} não possui legendas disponíveis.")

        if entries:
            print(f"[✔] Transcrição obtida ({len(entries)} falas encontradas)", flush=True)

        if custom_start is not None and custom_end is not None:
            print(f"[🎯] Usando recorte customizado: {custom_start:.1f}s até {custom_end:.1f}s", flush=True)
            chosen_title = (custom_title or f"A Revelação Inesperada | {meta['channel']}")[:55]
            chosen_hook = (custom_hook or custom_title or "VEJA ATÉ O FINAL")[:45]
            clips = [{
                "start_seconds": float(custom_start),
                "end_seconds": float(custom_end),
                "title": chosen_title,
                "hook_text": chosen_hook,
                "description": f"Trecho marcante de {meta['title']} ({meta['channel']}).\n\nCréditos ao canal original: {meta['channel']}\n\n#Shorts #Cortes #Podcast",
                "tags": ["shorts", "cortes", "podcast", "viral", meta["channel"].lower()],
                "viral_reason": "Momento selecionado com alto impacto emocional e engajamento."
            }]
        elif GEMINI_API_KEY:
            print("[🤖] Analisando narrativa com IA (Gemini)...", flush=True)
            formatted_txt = format_transcript_for_prompt(entries)
            clips = find_viral_moments_with_gemini(
                transcript_text=formatted_txt,
                video_title=meta["title"],
                channel_name=meta["channel"],
                max_clips=max_clips,
            )
        else:
            print("[⚡] Usando algoritmo de retenção e palavras-chave...", flush=True)
            clips = find_viral_moments_heuristic(
                entries=entries,
                video_title=meta["title"],
                channel_name=meta["channel"],
                max_clips=max_clips,
            )

        if not clips:
            raise RuntimeError("Nenhum trecho viral foi identificado.")

        results = []
        timestamp_slug = int(time.time())
        yt_service = YouTubeService() if upload_to_youtube else None

        for idx, clip in enumerate(clips, start=1):
            start = clip["start_seconds"]
            end = clip["end_seconds"]
            duration = end - start
            title = clip.get("title", f"Corte {idx} - {meta['title']}")[:100]
            print(f"\n[✂] Processando Corte {idx}/{len(clips)}: {title} ({duration:.1f}s)", flush=True)

            clip_temp_dir = TEMP_DIR / f"clip_{timestamp_slug}_{idx}"
            clip_temp_dir.mkdir(parents=True, exist_ok=True)

            raw_clip_path = str(clip_temp_dir / "raw_clip.mp4")
            ass_path = str(clip_temp_dir / "subtitles.ass")
            final_video_name = f"short_{timestamp_slug}_{idx}.mp4"
            final_video_path = str(OUTPUT_DIR / final_video_name)

            download_ok = download_clip_segment(
                url=youtube_url,
                start_sec=start,
                end_sec=end,
                output_raw_path=raw_clip_path,
            )
            if not download_ok:
                print(f"[!] Falha ao baixar corte {idx}. Pulando...", flush=True)
                continue

            build_ass_subtitles(
                entries=entries,
                clip_start=start,
                clip_end=end,
                output_ass_path=ass_path,
                headline_text=clip.get("hook_text"),
            )

            render_ok = render_short_916_with_subtitles(
                raw_video_path=raw_clip_path,
                ass_subtitle_path=ass_path,
                output_path=final_video_path,
                music_path=music_path,
            )

            if not render_ok:
                print(f"[!] Falha ao renderizar corte {idx}.", flush=True)
                continue

            print(f"[✔] Vídeo Shorts criado: {final_video_path}", flush=True)

            uploaded_yt_id = None
            if upload_to_youtube and yt_service:
                try:
                    print(f"[📡] Enviando para o YouTube ({privacy})...", flush=True)
                    upload_res = yt_service.upload_video(
                        file_path=final_video_path,
                        title=title,
                        description=clip.get("description", f"Corte de {meta['title']}\n\n#Shorts"),
                        tags=clip.get("tags", ["shorts", "cortes", "podcast"]),
                        privacy_status=privacy,
                        publish_at=publish_at,
                        made_for_kids=False,
                    )
                    uploaded_yt_id = upload_res.get("id")
                    print(f"[🎉] Publicado no YouTube! ID: {uploaded_yt_id}", flush=True)
                    print(f"🔗 Link: https://youtu.be/{uploaded_yt_id}", flush=True)
                except Exception as e:
                    print(f"[!] Erro no upload para o YouTube: {e}", flush=True)

            results.append({
                "clip_number": idx,
                "title": title,
                "start": start,
                "end": end,
                "duration": duration,
                "file_path": final_video_path,
                "youtube_id": uploaded_yt_id,
                "youtube_link": f"https://youtu.be/{uploaded_yt_id}" if uploaded_yt_id else None,
                "viral_reason": clip.get("viral_reason", ""),
            })

            # Limpar arquivos temporários do corte processado
            try:
                shutil.rmtree(clip_temp_dir, ignore_errors=True)
            except Exception:
                pass

        return results
