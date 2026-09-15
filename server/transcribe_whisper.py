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
import unicodedata
from collections import Counter

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

def remover_acentos(txt):
    """Remove marcas diacríticas preservando letras base (ex: 'á' -> 'a', 'ç' -> 'c')."""
    return ''.join(c for c in unicodedata.normalize('NFD', txt) if unicodedata.category(c) != 'Mn')

CONTRACOES_PT = {
    'pra': 'para', 'pro': 'para o', 'pras': 'para as', 'pros': 'para os',
    'ta': 'esta', 'tao': 'estao', 'tava': 'estava', 'to': 'estou',
    'ce': 'voce', 'ces': 'voces', 'ne': 'nao e',
    'num': 'em um', 'numa': 'em uma', 'nuns': 'em uns', 'numas': 'em umas',
    'dum': 'de um', 'duma': 'de uma', 'duns': 'de uns', 'dumas': 'de umas',
}

NUM_MAP_PT = {
    '0': 'zero', '1': 'um', '2': 'dois', '3': 'tres', '4': 'quatro',
    '5': 'cinco', '6': 'seis', '7': 'sete', '8': 'oito', '9': 'nove',
    '10': 'dez', '100': 'cem', '140': 'cento e quarenta', '1000': 'mil'
}

def tokenizar(texto):
    """Divide o texto em palavras preservando acentos e pontuação original."""
    return re.findall(r"\S+", texto)

def limpar_todas_tags(texto):
    """
    Remove completamente qualquer tag de áudio, direção de voz, cena ou anotação entre colchetes ou parênteses.
    Garante que a legenda SRT gerada nunca contenha [tags], [thoughtful], [gasp], [música], etc.
    """
    if not texto:
        return ""
    # 1. Remove qualquer conteúdo entre colchetes [tag], [direção de voz], [SCENE...], etc.
    t = re.sub(r"\[[\s\S]*?\]", " ", texto)
    # 2. Remove tags em estilo XML/HTML: <pause...>, <...>, etc.
    t = re.sub(r"<[^>]+>", " ", t)
    # 3. Remove anotações comuns de áudio entre parênteses: (música), (risos), (pausa), etc.
    t = re.sub(r"\((?:m[úu]sica|risos?|aplausos?|palmas?|som|tosse|suspiro|gasp|whisper\w*|laughter|applause|music|singing|pausa|sil[êe]ncio|barulho)\)", " ", t, flags=re.IGNORECASE)
    # 4. Remove colchetes residuais caso algum tenha ficado despareado
    t = t.replace("[", "").replace("]", "")
    # 5. Normaliza espaços mantendo pontuação limpa
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\s+([,.:;!?…])", r"\1", t)
    return t.strip()

def remover_colchetes(texto):
    """Remove indicações de cena e audio tags como [whispering], [gasp], [pause]."""
    return limpar_todas_tags(texto)

def _normalizar_palavra_alinhamento(p):
    """Normaliza para comparação fonética/textual flexível."""
    p_sem_acento = remover_acentos(p.lower())
    p_limpa = re.sub(r"[^a-z0-9]", "", p_sem_acento)
    p_limpa = NUM_MAP_PT.get(p_limpa, p_limpa)
    return CONTRACOES_PT.get(p_limpa, p_limpa)

def _tokenizar_limpo(texto):
    """
    Tokeniza separando travessões, hífens e barras coladas em palavras,
    mantendo pontuação e maiúsculas originais de cada termo.
    """
    texto_sem_tags = remover_colchetes(texto)
    # Separa travessões e hífens grudados (ex: 'palavra—outra' -> 'palavra — outra')
    texto_espacado = re.sub(r"([—–\-_/]+)", r" \1 ", texto_sem_tags)
    tokens_brutos = re.findall(r"\S+", texto_espacado)
    tokens_validos = []
    for t in tokens_brutos:
        if re.search(r"[a-zA-Z0-9À-ÿ]", t):
            tokens_validos.append(t)
    return tokens_validos

def _alinhar_palavras_audio(palavras_ref_originais, palavras_aud_whisper):
    """
    Alinha as palavras faladas no áudio com o texto de referência do usuário.
    - Preserva rigorosamente os timestamps em milissegundos do Whisper.
    - Aplica caixa alta, pontuação e ortografia do texto do roteiro.
    - Suporta modo subsegmento (ex: bloco individual de 100 palavras com roteiro completo de 1.400 palavras).
    - Retorna palavras_finais_alinhadas com timestamps reais.
    """
    n_ref = len(palavras_ref_originais)
    n_aud = len(palavras_aud_whisper)
    if n_aud == 0:
        return []
    if n_ref == 0:
        return [(w[0], w[1], w[2]) for w in palavras_aud_whisper]

    # Prepara chaves normalizadas para comparação
    keys_ref = [_normalizar_palavra_alinhamento(p) for p in palavras_ref_originais]
    keys_aud = [_normalizar_palavra_alinhamento(w[0]) for w in palavras_aud_whisper]

    # Índice invertido da referência: palavra -> [posições]
    pos_ref = {}
    for i, k in enumerate(keys_ref):
        if not k:
            continue
        if k not in pos_ref:
            pos_ref[k] = []
        pos_ref[k].append(i)

    # Identifica se o áudio é significativamente menor que o texto (ex: bloco único com roteiro completo)
    is_subsegment = n_aud < n_ref * 0.65

    start_offset = 0
    if is_subsegment:
        candidatos_inicio = []
        for ja in range(min(20, n_aud)):
            ka = keys_aud[ja]
            if ka and ka in pos_ref:
                for ir in pos_ref[ka]:
                    if ir >= ja:
                        candidatos_inicio.append(ir - ja)
        if candidatos_inicio:
            c_inicio = Counter(candidatos_inicio)
            start_offset = c_inicio.most_common(1)[0][0]

    # Alinhamento monotônico temporal (janela deslizante)
    ancoras = {}  # j_aud -> i_ref
    ultimo_i = max(0, start_offset - 5)
    janela_busca = max(100, int(n_aud * 0.35))

    for j, ka in enumerate(keys_aud):
        if not ka:
            continue

        if is_subsegment:
            i_esp = start_offset + j
        else:
            i_esp = int(round(j * (n_ref / max(n_aud, 1))))

        melhor_i = None
        melhor_dist = float('inf')

        # 1. Match exato normalizado
        if ka in pos_ref:
            for ir in pos_ref[ka]:
                if ir >= ultimo_i - 2:
                    dist = abs(ir - i_esp)
                    if dist < janela_busca and dist < melhor_dist:
                        melhor_dist = dist
                        melhor_i = ir

        # 2. Match fuzzy de fallback (flexão plural, conjugação ou pequena variação fonética)
        if melhor_i is None and len(ka) >= 4:
            raio = min(15, janela_busca // 2)
            i_ini = max(ultimo_i, i_esp - raio)
            i_fim = min(n_ref, i_esp + raio)
            for ir in range(i_ini, i_fim):
                kr = keys_ref[ir]
                if kr and len(kr) >= 4 and abs(len(kr) - len(ka)) <= 3:
                    if difflib.SequenceMatcher(None, ka, kr).quick_ratio() >= 0.82:
                        melhor_i = ir
                        break

        if melhor_i is not None:
            ancoras[j] = melhor_i
            ultimo_i = max(ultimo_i, melhor_i)

    # Constrói as palavras finais preservando pontuação/maiúsculas do roteiro
    palavras_finais = []
    for j, (w_aud, t0, t1) in enumerate(palavras_aud_whisper):
        if j in ancoras:
            ir = ancoras[j]
            palavra_formatada = limpar_todas_tags(palavras_ref_originais[ir])
            if palavra_formatada:
                palavras_finais.append((palavra_formatada, t0, t1))
        else:
            w_limpo = limpar_todas_tags(w_aud)
            if w_limpo:
                palavras_finais.append((w_limpo, t0, t1))

    return palavras_finais

def _gerar_blocos_srt(audio_path, texto_usuario="", max_caracteres=42):
    """
    Gera blocos sincronizados contendo (t_inicio, t_fim, texto_bloco)
    respeitando as regras de quebra por fim de frase (.!?…), pausa de áudio ou limite de caracteres.
    """
    modelo = obter_modelo()
    segments, info = modelo.transcribe(audio_path, word_timestamps=True)

    palavras_audio = []
    for seg in segments:
        for w in (seg.words or []):
            w_limpo = limpar_todas_tags(w.word.strip())
            if w_limpo:
                palavras_audio.append((w_limpo, w.start, w.end))

    n_audio = len(palavras_audio)
    if n_audio == 0:
        raise ValueError("Não foi possível transcrever o áudio. Verifique se o arquivo tem fala audível.")

    texto_usuario = limpar_todas_tags(texto_usuario)
    texto_usuario_informado = bool(texto_usuario and texto_usuario.strip())
    if texto_usuario_informado:
        palavras_texto = _tokenizar_limpo(texto_usuario)
        palavras_finais = _alinhar_palavras_audio(palavras_texto, palavras_audio)
    else:
        palavras_finais = [(w[0], w[1], w[2]) for w in palavras_audio]

    info_meta = {
        'texto_usuario_informado': texto_usuario_informado
    }

    blocos = []
    atual = []
    atual_n = 0
    inicio_bloco = None
    fim_bloco = None

    for i, (palavra, t_inicio, t_fim) in enumerate(palavras_finais):
        if inicio_bloco is None:
            inicio_bloco = t_inicio
        atual.append(palavra)
        atual_n += len(palavra) + 1
        fim_bloco = t_fim
        termina_frase = palavra[-1] in ".!?…"

        # Pausa natural no áudio (ex: silêncio > 0.8s) fecha o bloco para não reter legenda na tela
        pausa_longa = False
        if i + 1 < len(palavras_finais):
            prox_t0 = palavras_finais[i + 1][1]
            if prox_t0 - t_fim > 0.8:
                pausa_longa = True

        if (termina_frase or atual_n >= max_caracteres or pausa_longa) and atual:
            texto_bloco = " ".join(atual)
            blocos.append((inicio_bloco, fim_bloco, texto_bloco))
            atual = []
            atual_n = 0
            inicio_bloco = None

    if atual:
        texto_bloco = " ".join(atual)
        blocos.append((inicio_bloco, fim_bloco, texto_bloco))

    duracao = getattr(info, "duration", None) or (blocos[-1][1] if blocos else 0.0)
    return blocos, duracao, info_meta

def _formatar_srt(blocos):
    """Monta o formato final .srt numerado e com timestamps, sem nenhuma tag de áudio."""
    linhas_srt = []
    idx_real = 1
    for (t0, t1, texto_bloco) in blocos:
        texto_limpo = limpar_todas_tags(texto_bloco)
        if not texto_limpo:
            continue
        linhas_srt.append(str(idx_real))
        linhas_srt.append(f"{formatar_tempo(t0)} --> {formatar_tempo(t1)}")
        linhas_srt.append(texto_limpo)
        linhas_srt.append("")
        idx_real += 1
    return "\n".join(linhas_srt).strip() + "\n"

def gerar_srt_whisper(audio_path, texto_referencia="", max_caracteres=42, retornar_meta=False):
    """
    Função principal chamada pela ponte para gerar a legenda SRT completa.
    """
    texto_referencia = limpar_todas_tags(texto_referencia)
    blocos, _duracao, info_meta = _gerar_blocos_srt(audio_path, texto_referencia, max_caracteres=max_caracteres)
    srt_conteudo = _formatar_srt(blocos)
    if retornar_meta:
        return srt_conteudo, info_meta
    return srt_conteudo
