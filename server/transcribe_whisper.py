# -*- coding: utf-8 -*-
"""
Módulo de Transcrição e Geração de Legendas SRT
Implementado com base no algoritmo do 'app-legendas' (C:\\Projetos\\app-legendas):
- Remoção de tags entre colchetes [direções de voz/áudio tags]
- Alinhamento de texto com SequenceMatcher e âncoras locais
- Preservação 100% fiel da pontuação, acentos e formatação original do roteiro
- Quebra natural de legendas por pontuação de frase (. ! ? …) e limite de 42 caracteres
- Detecção automática de GPU (CUDA) com teste em silêncio e fallback para CPU (int8)
"""
import os
import sys
import re
import difflib
import bisect

# 1. Configura cache do Hugging Face para o Whisper
hf_cache = os.path.join(r"C:\Projetos\app-legendas", "runtime", "hf_cache")
if os.path.isdir(hf_cache) and "HF_HOME" not in os.environ:
    os.environ["HF_HOME"] = hf_cache

# 2. Configura DLLs do CUDA (cuBLAS, cuDNN, etc.)
def _configurar_cuda():
    try:
        import site
        for base in site.getsitepackages():
            for sub in ("nvidia/cublas/bin", "nvidia/cudnn/bin",
                        "nvidia/cuda_runtime/bin", "nvidia/cuda_nvrtc/bin"):
                d = os.path.join(base, sub)
                if os.path.isdir(d):
                    os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
                    try:
                        os.add_dll_directory(d)
                    except Exception:
                        pass
    except Exception:
        pass

_configurar_cuda()

from faster_whisper import WhisperModel

_modelo = None

def obter_modelo(modelo_nome="small"):
    global _modelo
    if _modelo is not None:
        return _modelo
    print(f"[Whisper] Carregando modelo '{modelo_nome}' (primeira vez pode demorar)...", file=sys.stderr)
    _configurar_cuda()
    try:
        print("[Whisper] Tentando GPU (CUDA)...", file=sys.stderr)
        modelo_cuda = WhisperModel(modelo_nome, device="cuda", compute_type="float16")
        # Valida se o CUDA realmente responde com um tensor de silêncio
        import numpy as np
        silencio = np.zeros(16000, dtype=np.float32)
        list(modelo_cuda.transcribe(silencio, word_timestamps=True)[0])
        _modelo = modelo_cuda
        print("[Whisper] Modelo carregado na GPU com sucesso!", file=sys.stderr)
    except Exception as e:
        print(f"[Whisper] GPU falhou ({e}). Usando CPU (int8)...", file=sys.stderr)
        _modelo = WhisperModel(modelo_nome, device="cpu", compute_type="int8")
        print("[Whisper] Modelo carregado na CPU.", file=sys.stderr)
    return _modelo

def formatar_tempo(segundos):
    """Converte segundos para o formato SRT padrão (HH:MM:SS,mmm)."""
    if segundos < 0:
        segundos = 0
    ms = int(round(segundos * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def tokenizar(texto):
    """Divide o texto em palavras preservando acentos e pontuação original."""
    return re.findall(r"\S+", texto)

def remover_colchetes(texto):
    """Remove indicações de cena e audio tags como [whispering], [gasp], [pause]."""
    return re.sub(r"\[[^\]]*\]", "", texto)

def _normalizar_palavra(p):
    """Normaliza para comparação fonética/textual removendo pontuações externas."""
    return re.sub(r"^[^\wÀ-ÿ]+|[^\wÀ-ÿ]+$", "", p).lower()

def _mapear_indices_audio(palavras_texto, palavras_audio):
    """
    Casa cada palavra do texto de referência com a palavra dita no áudio
    por CONTEÚDO (não só posição) usando SequenceMatcher:
    Os trechos coincidentes viram âncoras reais de timestamp, e os intervalos
    são interpolados localmente, garantindo que um erro isolado do Whisper
    não desalinhe o restante do áudio.
    """
    n_texto = len(palavras_texto)
    n_audio = len(palavras_audio)
    if n_texto == 0 or n_audio == 0:
        return []
    chaves_texto = [_normalizar_palavra(p) for p in palavras_texto]
    chaves_audio = [_normalizar_palavra(w[0]) for w in palavras_audio]
    sm = difflib.SequenceMatcher(None, chaves_texto, chaves_audio, autojunk=False)
    ancoras = {}
    for bloco in sm.get_matching_blocks():
        for k in range(bloco.size):
            ancoras[bloco.a + k] = bloco.b + k
    if not ancoras:
        return [int(round(i * (n_audio - 1) / max(n_texto - 1, 1))) for i in range(n_texto)]
    indices_ancorados = sorted(ancoras)
    mapa = []
    for i in range(n_texto):
        if i in ancoras:
            mapa.append(ancoras[i])
            continue
        pos = bisect.bisect_left(indices_ancorados, i)
        if pos == 0:
            mapa.append(ancoras[indices_ancorados[0]])
        elif pos == len(indices_ancorados):
            mapa.append(ancoras[indices_ancorados[-1]])
        else:
            i_ant, i_prox = indices_ancorados[pos - 1], indices_ancorados[pos]
            b_ant, b_prox = ancoras[i_ant], ancoras[i_prox]
            frac = (i - i_ant) / (i_prox - i_ant)
            mapa.append(int(round(b_ant + frac * (b_prox - b_ant))))
    return mapa

def _gerar_blocos_srt(audio_path, texto_usuario="", max_caracteres=42):
    """
    Gera blocos sincronizados contendo (t_inicio, t_fim, texto_bloco)
    respeitando as regras de quebra por fim de frase (.!?…) ou limite de caracteres.
    """
    modelo = obter_modelo()
    segments, info = modelo.transcribe(audio_path, word_timestamps=True)

    palavras_audio = []
    for seg in segments:
        for w in (seg.words or []):
            palavras_audio.append((w.word.strip(), w.start, w.end))

    n_audio = len(palavras_audio)
    if n_audio == 0:
        raise ValueError("Não foi possível transcrever o áudio. Verifique se o arquivo tem fala audível.")

    if texto_usuario and texto_usuario.strip():
        texto_limpo = remover_colchetes(texto_usuario).strip()
        palavras_texto = tokenizar(texto_limpo)
    else:
        palavras_texto = [w[0] for w in palavras_audio]

    if not palavras_texto:
        palavras_texto = [w[0] for w in palavras_audio]

    mapa_indices = _mapear_indices_audio(palavras_texto, palavras_audio)
    blocos = []
    atual = []
    atual_n = 0
    inicio_bloco = None
    fim_bloco = None

    for i, palavra in enumerate(palavras_texto):
        ia = min(mapa_indices[i], n_audio - 1)
        t_inicio = palavras_audio[ia][1]
        t_fim = palavras_audio[ia][2]
        if inicio_bloco is None:
            inicio_bloco = t_inicio
        atual.append(palavra)
        atual_n += len(palavra) + 1
        fim_bloco = t_fim
        termina_frase = palavra[-1] in ".!?…"
        if (termina_frase or atual_n >= max_caracteres) and atual:
            texto_bloco = " ".join(atual)
            blocos.append((inicio_bloco, fim_bloco, texto_bloco))
            atual = []
            atual_n = 0
            inicio_bloco = None

    if atual:
        texto_bloco = " ".join(atual)
        blocos.append((inicio_bloco, fim_bloco, texto_bloco))

    duracao = getattr(info, "duration", None) or (blocos[-1][1] if blocos else 0.0)
    return blocos, duracao

def _formatar_srt(blocos):
    """Monta o formato final .srt numerado e com timestamps."""
    linhas_srt = []
    for idx, (t0, t1, texto_bloco) in enumerate(blocos, 1):
        linhas_srt.append(str(idx))
        linhas_srt.append(f"{formatar_tempo(t0)} --> {formatar_tempo(t1)}")
        linhas_srt.append(texto_bloco)
        linhas_srt.append("")
    return "\n".join(linhas_srt).strip() + "\n"

def gerar_srt_whisper(audio_path, texto_referencia="", max_caracteres=42):
    """
    Função principal chamada pela ponte para gerar a legenda SRT completa.
    """
    blocos, _duracao = _gerar_blocos_srt(audio_path, texto_referencia, max_caracteres=max_caracteres)
    return _formatar_srt(blocos)
