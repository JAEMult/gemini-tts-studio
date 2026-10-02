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

# Padrões conhecidos de alucinações e metadados de raspagem do Whisper (ex: créditos comunitários do YouTube no silêncio)
PADRAO_ALUCINACOES_WHISPER = re.compile(
    r'(?i)\b('
    r'legendas?\s+pela\s+comunidade\s+(?:de\s+)?amara\.org|'
    r'subt[ií]tulos\s+por\s+la\s+comunidad\s+(?:de\s+)?amara\.org|'
    r'subtitles\s+by\s+the\s+amara\.org\s+community|'
    r'sous-titres\s+par\s+la\s+communaut[ée]\s+d[\'’]amara\.org|'
    r'amara\.org|'
    r'opensubtitles(?:\.org)?|'
    r'visualiza[çc][ãa]o:\s*amara|'
    r'legendas?\s+da\s+comunidade\b|'
    r'transcri[çc][ãa]o\s+pela\s+comunidade\b|'
    r'legendado\s+pela\s+comunidade\b'
    r')\b'
)

def eh_alucinacao_whisper(texto, texto_referencia=""):
    """
    Detecta se um bloco de texto é uma alucinação clássica do Whisper ou crédito fantasma de raspagem.
    Se o usuário explicitamente colocou a palavra no texto de referência, não descarta.
    """
    if not texto or not str(texto).strip():
        return False
    t_limpo = str(texto).strip().lower()
    if PADRAO_ALUCINACOES_WHISPER.search(t_limpo):
        ref_limpo = (texto_referencia or "").lower()
        if "amara" not in ref_limpo and "opensubtitles" not in ref_limpo:
            return True
    return False

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

def normalizar_gramatica_srt(texto):
    if not texto:
        return ""
    t = texto
    # Correções de nomes bíblicos/históricos e erros fonéticos frequentes do Whisper
    t = re.sub(r'\bZBD\s+o\b', 'Zebedeu', t, flags=re.IGNORECASE)
    t = re.sub(r'\bZBD\b', 'Zebedeu', t)
    t = re.sub(r'\bZbdl\b', 'Zebedeu', t, flags=re.IGNORECASE)
    t = re.sub(r'\bCebedeo\b', 'Zebedeo', t, flags=re.IGNORECASE)
    t = re.sub(r'\bA\s+Dolecência\b', 'Adolescência', t, flags=re.IGNORECASE)
    t = re.sub(r'\ba\s+dolescência\b', 'Adolescência', t)
    t = re.sub(r'\bAdolescents\b', 'Adolescence', t)
    t = re.sub(r'\bComo\s+o\s+vidro\s+Anaxando\b', 'Comovido, Anaxando', t, flags=re.IGNORECASE)
    t = re.sub(r'\bComo\s+o\s+vidro\s+Anashando\b', 'Comovido, Anaxando', t, flags=re.IGNORECASE)
    t = re.sub(r'\bAnashando\b', 'Anaxando', t, flags=re.IGNORECASE)
    t = re.sub(r'\bSevereia\b', 'Cesareia', t, flags=re.IGNORECASE)
    t = re.sub(r'\bCésarêia\b', 'Cesareia', t, flags=re.IGNORECASE)
    t = re.sub(r'\bCesaréia\b', 'Cesareia', t, flags=re.IGNORECASE)
    t = re.sub(r'\bGhosnode\b', 'Gonod', t, flags=re.IGNORECASE)
    t = re.sub(r'\bGounad\b', 'Gonod', t, flags=re.IGNORECASE)
    t = re.sub(r'\bGhanad\b', 'Gonod', t, flags=re.IGNORECASE)
    t = re.sub(r'\bGannad\b', 'Ganid', t, flags=re.IGNORECASE)
    t = re.sub(r'\bGannet\b', 'Ganid', t, flags=re.IGNORECASE)
    t = re.sub(r'\bGonnad\b', 'Ganid', t, flags=re.IGNORECASE)
    t = re.sub(r'\bGanide\b', 'Ganid', t, flags=re.IGNORECASE)
    t = re.sub(r'\bGhanide\b', 'Ganid', t, flags=re.IGNORECASE)
    t = re.sub(r'\ba\s+ganite\b', 'Ganid', t, flags=re.IGNORECASE)
    t = re.sub(r'\bMelchisedec\b', 'Melquisedeque', t, flags=re.IGNORECASE)
    t = re.sub(r'\bMelchizedek\b', 'Melquisedec', t, flags=re.IGNORECASE)
    t = re.sub(r'\bSirene\b', 'Cirene', t, flags=re.IGNORECASE)
    t = re.sub(r'\bSiren\b', 'Cirene', t, flags=re.IGNORECASE)
    t = re.sub(r'\bse\s+mão\s+e\s+Judé\b', 'Simão e Judá', t, flags=re.IGNORECASE)
    t = re.sub(r'\bse\s+mão\s+e\s+Judá\b', 'Simão e Judá', t, flags=re.IGNORECASE)
    t = re.sub(r'\bse\s+mão\b', 'Simão', t, flags=re.IGNORECASE)
    t = re.sub(r'\bVim\s+um\b', 'Vinham', t, flags=re.IGNORECASE)
    t = re.sub(r'\bSalami\b', 'Salome', t)
    t = re.sub(r'\bInscreva\s*-\s*se\b', 'Inscreva-se', t, flags=re.IGNORECASE)
    t = re.sub(r'\bInscreva\s+se\b', 'Inscreva-se', t, flags=re.IGNORECASE)
    t = re.sub(r'\bTornou\s+se\b', 'Tornou-se', t)
    t = re.sub(r'\btornou\s+se\b', 'tornou-se', t)
    t = re.sub(r'\bDiz\s+se,\s*apenas\b', 'Disse apenas', t, flags=re.IGNORECASE)
    t = re.sub(r'\bDiz\s+se\b', 'Disse', t, flags=re.IGNORECASE)
    t = re.sub(r'\bà\s+Beiramar\b', 'à beira-mar', t, flags=re.IGNORECASE)
    t = re.sub(r'\bBeiramar\b', 'beira-mar', t, flags=re.IGNORECASE)
    t = re.sub(r'\bDalindiante\b', 'Dali em diante', t, flags=re.IGNORECASE)
    t = re.sub(r'\bcontia\b', 'quantia', t, flags=re.IGNORECASE)
    t = re.sub(r'\bCaffarnaum\b', 'Cafarnaum', t, flags=re.IGNORECASE)
    t = re.sub(r'\ba\s+Caffarnaum\b', 'Cafarnaum', t, flags=re.IGNORECASE)
    t = re.sub(r'\bmentário\s+inesperado\b', 'mentor inesperado', t, flags=re.IGNORECASE)
    t = re.sub(r'\blidar\b', 'lhe dar', t, flags=re.IGNORECASE)
    t = re.sub(r'\ba\s+via\b', 'havia', t, flags=re.IGNORECASE)
    t = re.sub(r'\bvinha\s+aí\b', 'vem aí', t, flags=re.IGNORECASE)
    t = re.sub(r'\bBetzayda\b', 'Betsaida', t, flags=re.IGNORECASE)
    t = re.sub(r'\bTiberius\b', 'Tiberias', t, flags=re.IGNORECASE)
    t = re.sub(r'\bKapua\b', 'Capua', t, flags=re.IGNORECASE)
    t = re.sub(r'\bJapa\b', 'Joppa', t, flags=re.IGNORECASE)
    t = re.sub(r'\bGrigo\b', 'grego', t, flags=re.IGNORECASE)
    t = re.sub(r'\bamphitheatro\b', 'anfiteatro', t, flags=re.IGNORECASE)
    t = re.sub(r'\bJudéia\b', 'Judeia', t, flags=re.IGNORECASE)
    t = re.sub(r'\bé\s+assista\b', 'e assista', t, flags=re.IGNORECASE)
    t = re.sub(r'\bSuscríbete\s*-\s*te\b', 'Suscríbete', t, flags=re.IGNORECASE)
    t = re.sub(r'\bSuscríbete\s*,\s*', 'Suscríbete, ', t, flags=re.IGNORECASE)
    t = re.sub(r'\bSubscribe\s*,\s*', 'Subscribe, ', t, flags=re.IGNORECASE)
    t = re.sub(r'[ \t]+', ' ', t)
    t = re.sub(r'\s+([,.:;!?…])', r'\1', t)
    return t.strip()

def _gerar_blocos_srt(audio_path, texto_usuario="", max_caracteres=42, progress_callback=None):
    """
    Gera blocos sincronizados contendo (t_inicio, t_fim, texto_bloco)
    respeitando as regras de quebra por fim de frase (.!?…), pausa de áudio ou limite de caracteres.
    """
    if progress_callback:
        progress_callback("Carregando modelo Whisper na GPU...", pct=12)

    modelo = obter_modelo()

    if progress_callback:
        progress_callback("Iniciando transcrição de áudio com Whisper...", pct=15)

    segments, info = modelo.transcribe(audio_path, word_timestamps=True)

    duracao_total = getattr(info, 'duration', 0.0) or 1.0
    palavras_audio = []
    for seg in segments:
        seg_txt = getattr(seg, 'text', '') or ''
        if eh_alucinacao_whisper(seg_txt, texto_usuario):
            continue
        if progress_callback:
            pct = int(min(92, max(15, (seg.end / duracao_total) * 100)))
            progress_callback(f"Transcrevendo com Whisper ({seg.end:.1f}s / {duracao_total:.1f}s)...", pct=pct)
        for w in (seg.words or []):
            w_limpo = limpar_todas_tags(w.word.strip())
            if w_limpo and not eh_alucinacao_whisper(w_limpo, texto_usuario):
                palavras_audio.append((w_limpo, w.start, w.end))

    n_audio = len(palavras_audio)
    if n_audio == 0:
        raise ValueError("Não foi possível transcrever o áudio. Verifique se o arquivo tem fala audível.")

    if progress_callback:
        progress_callback("Alinhando legendas com o roteiro...", pct=94)

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

        # Pausa natural no áudio (ex: silêncio > 0.8s) fecha o bloco
        pausa_longa = False
        if i + 1 < len(palavras_finais):
            prox_t0 = palavras_finais[i + 1][1]
            if prox_t0 - t_fim > 0.8:
                pausa_longa = True

        # Prevenção ativa de palavras órfãs no fim da frase:
        # Se fechar agora for deixar apenas 1 ou 2 palavras sozinhas até o ponto final, segura no bloco atual
        deve_fechar_por_tamanho = False
        if atual_n >= max_caracteres and not termina_frase and not pausa_longa:
            restantes_na_frase = 0
            for j in range(i + 1, min(i + 8, len(palavras_finais))):
                restantes_na_frase += 1
                if palavras_finais[j][0][-1] in ".!?…":
                    break
            if restantes_na_frase > 2:
                deve_fechar_por_tamanho = True

        if (termina_frase or deve_fechar_por_tamanho or pausa_longa) and atual:
            texto_bloco = normalizar_gramatica_srt(" ".join(atual))
            if texto_bloco:
                blocos.append((inicio_bloco, fim_bloco, texto_bloco))
            atual = []
            atual_n = 0
            inicio_bloco = None

    if atual:
        texto_bloco = normalizar_gramatica_srt(" ".join(atual))
        if texto_bloco:
            blocos.append((inicio_bloco, fim_bloco, texto_bloco))

    duracao = getattr(info, "duration", None) or (blocos[-1][1] if blocos else 0.0)

    # Passada anti-órfã: funde palavras isoladas (< 3 palavras ou duração < 0.85s) com a fala anterior
    blocos_sem_orfas = []
    for b in blocos:
        t0, t1, txt = b
        txt = normalizar_gramatica_srt(limpar_todas_tags(txt))
        if not txt:
            continue
        words = txt.split()
        dur_cue = t1 - t0
        if len(words) <= 1 and dur_cue < 0.85 and blocos_sem_orfas:
            p_t0, p_t1, p_txt = blocos_sem_orfas[-1]
            blocos_sem_orfas[-1] = (p_t0, t1, normalizar_gramatica_srt(f"{p_txt} {txt}"))
        elif len(words) <= 2 and dur_cue < 0.65 and blocos_sem_orfas:
            p_t0, p_t1, p_txt = blocos_sem_orfas[-1]
            blocos_sem_orfas[-1] = (p_t0, t1, normalizar_gramatica_srt(f"{p_txt} {txt}"))
        elif len(words) <= 1 and dur_cue < 0.85 and not blocos_sem_orfas and len(blocos) > 1:
            # Palavra órfã inicial (ex: 'Explicou:') - aguarda para fundir na próxima se necessário
            blocos_sem_orfas.append((t0, t1, txt))
        else:
            blocos_sem_orfas.append((t0, t1, txt))
    blocos = blocos_sem_orfas

    # Blindagem temporal estrita (Garante 0 amontoadas, 0 retrocessos e durações legíveis)
    blocos_seguros = []
    for idx_b, (t0, t1, txt) in enumerate(blocos):
        txt_limpo = normalizar_gramatica_srt(limpar_todas_tags(txt))
        if not txt_limpo:
            continue
        if blocos_seguros:
            prev_t0, prev_t1, _ = blocos_seguros[-1]
            if t0 < prev_t0 + 0.25:
                t0 = round(prev_t0 + 0.30, 3)
            if t0 < prev_t1 - 0.05:
                t0 = round(prev_t1 + 0.02, 3)
        dur_min = max(0.5, min(7.5, len(txt_limpo) * 0.04))
        if t1 <= t0 or (t1 - t0) < dur_min:
            t1 = round(t0 + dur_min, 3)
        elif (t1 - t0) > 8.0:
            dur_ideal = max(3.0, min(7.5, len(txt_limpo) * 0.075))
            t1 = round(t0 + dur_ideal, 3)
        blocos_seguros.append((t0, t1, txt_limpo))
    blocos = blocos_seguros

    return blocos, duracao, info_meta

def _formatar_srt(blocos):
    """Monta o formato final .srt numerado e com timestamps, sem nenhuma tag de áudio."""
    linhas_srt = []
    idx_real = 1
    for (t0, t1, texto_bloco) in blocos:
        if eh_alucinacao_whisper(texto_bloco):
            continue
        texto_limpo = limpar_todas_tags(texto_bloco)
        if eh_alucinacao_whisper(texto_limpo):
            continue
        # Normalização gramatical final
        texto_limpo = re.sub(r'\bInscreva\s*-\s*se\b', 'Inscreva-se', texto_limpo, flags=re.IGNORECASE)
        texto_limpo = re.sub(r'\bInscreva\s+se\b', 'Inscreva-se', texto_limpo, flags=re.IGNORECASE)
        texto_limpo = re.sub(r'\bTornou\s+se\b', 'Tornou-se', texto_limpo)
        texto_limpo = re.sub(r'\btornou\s+se\b', 'tornou-se', texto_limpo)
        texto_limpo = re.sub(r'\bDiz\s+se,\s*apenas\b', 'Disse apenas', texto_limpo, flags=re.IGNORECASE)
        texto_limpo = re.sub(r'\bDiz\s+se\b', 'Disse', texto_limpo, flags=re.IGNORECASE)
        texto_limpo = re.sub(r'\bà\s+Beiramar\b', 'à beira-mar', texto_limpo, flags=re.IGNORECASE)
        texto_limpo = re.sub(r'\bconvidas\b', 'com vidas', texto_limpo, flags=re.IGNORECASE)
        texto_limpo = re.sub(r'\bé\s+assista\b', 'e assista', texto_limpo, flags=re.IGNORECASE)
        texto_limpo = re.sub(r'[ \t]+', ' ', texto_limpo).strip()
        if not texto_limpo:
            continue
        linhas_srt.append(str(idx_real))
        linhas_srt.append(f"{formatar_tempo(t0)} --> {formatar_tempo(t1)}")
        linhas_srt.append(texto_limpo)
        linhas_srt.append("")
        idx_real += 1
    return "\n".join(linhas_srt).strip() + "\n"

def parse_srt(srt_conteudo):
    """
    Faz o parse completo de um conteúdo SRT em lista de blocos estruturados:
    [{ 'idx': int, 't0': float, 't1': float, 'texto': str, 'duracao': float }, ...]
    """
    blocos = []
    if not srt_conteudo or not srt_conteudo.strip():
        return blocos

    padrao = re.compile(
        r'(\d+)\s*\n(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*\n([\s\S]*?)(?=\n\s*\n\d+\s*\n|\Z)',
        re.MULTILINE
    )

    def _tempo_para_seg(ts):
        ts = ts.replace(',', '.')
        p = ts.split(':')
        return int(p[0]) * 3600 + int(p[1]) * 60 + float(p[2])

    for m in padrao.finditer(srt_conteudo.strip()):
        try:
            idx = int(m.group(1))
            t0 = _tempo_para_seg(m.group(2))
            t1 = _tempo_para_seg(m.group(3))
            texto = " ".join(m.group(4).strip().split())
            if not texto:
                continue
            blocos.append({
                'idx': idx,
                't0': round(t0, 3),
                't1': round(t1, 3),
                'duracao': round(max(0.0, t1 - t0), 3),
                'texto': texto
            })
        except Exception:
            continue
    return blocos

def auditar_srt(srt_conteudo, duracao_audio_seg=None):
    """
    Audita matematicamente a integridade física e temporal de uma legenda SRT.
    Retorna métricas exatas: amontoadas, micro/mega durações, CPS, cobertura e score 0-100.
    """
    blocos = parse_srt(srt_conteudo)
    total_cues = len(blocos)
    if total_cues == 0:
        return {
            'aprovado': False,
            'score': 0,
            'total_cues': 0,
            'amontoadas': 0,
            'detalhes_amontoadas': [],
            'micro_duras': 0,
            'mega_duras': 0,
            'retrocessos': 0,
            'cps_medio': 0.0,
            'cps_max': 0.0,
            'cobertura_audio_pct': 0.0,
            'problemas': ['Arquivo de legenda está vazio ou ilegível.'],
            'resumo': 'Arquivo SRT vazio.'
        }

    amontoadas = []
    micro_duras = []
    mega_duras = []
    retrocessos = []
    alucinacoes = []
    total_chars = 0
    total_tempo_fala = 0.0
    cps_max = 0.0

    for i in range(total_cues):
        b = blocos[i]
        dur = b['duracao']
        txt = b['texto']
        chars = len(txt)
        total_chars += chars
        total_tempo_fala += max(0.1, dur)

        if eh_alucinacao_whisper(txt):
            alucinacoes.append({'idx': b['idx'], 'txt': txt[:40]})

        cps = chars / max(0.1, dur)
        if cps > cps_max:
            cps_max = cps

        # Micro-durações (< 0.4s para frases com mais de uma palavra ou > 8 chars)
        if dur < 0.4 and (chars > 8 or ' ' in txt):
            micro_duras.append({'idx': b['idx'], 'dur': dur, 'txt': txt[:35]})

        # Mega-durações (> 8.0s)
        if dur > 8.0:
            mega_duras.append({'idx': b['idx'], 'dur': dur, 'txt': txt[:35]})

        if i > 0:
            b_ant = blocos[i - 1]
            diff_inicio = b['t0'] - b_ant['t0']

            # Retrocesso cronológico
            if diff_inicio < -0.01:
                retrocessos.append({
                    'idx': b['idx'],
                    'tempo': formatar_tempo(b['t0']),
                    'tempo_anterior': formatar_tempo(b_ant['t0']),
                    'txt': txt[:35]
                })

            # Amontoamento / Stacked cues (início colidindo em < 250ms)
            elif diff_inicio < 0.25:
                amontoadas.append({
                    'idx': b['idx'],
                    'tempo': formatar_tempo(b['t0']),
                    'tempo_anterior': formatar_tempo(b_ant['t0']),
                    'diff_ms': int(diff_inicio * 1000),
                    'txt': txt[:35]
                })

    cps_medio = round(total_chars / max(1.0, total_tempo_fala), 1)
    cps_max = round(cps_max, 1)

    cobertura_pct = 100.0
    if duracao_audio_seg and duracao_audio_seg > 0:
        ultimo_t1 = blocos[-1]['t1']
        cobertura_pct = round((ultimo_t1 / duracao_audio_seg) * 100, 1)

    # Cálculo do Score de Integridade (0 a 100)
    score = 100
    problemas = []

    if alucinacoes:
        score -= min(50, len(alucinacoes) * 25)
        problemas.append(f"{len(alucinacoes)} alucinação(ões) ou créditos fantasmas do Whisper detectados (ex: Amara.org).")

    if amontoadas:
        qtd = len(amontoadas)
        perda = min(60, qtd * 10)
        score -= perda
        problemas.append(f"{qtd} falas amontoadas no mesmo instante ou em < 250ms.")

    if retrocessos:
        score -= min(40, len(retrocessos) * 20)
        problemas.append(f"{len(retrocessos)} falas com ordem cronológica invertida.")

    if micro_duras:
        score -= min(20, len(micro_duras) * 2)
        problemas.append(f"{len(micro_duras)} falas com micro-duração (< 0.4s).")

    if mega_duras:
        score -= min(15, len(mega_duras) * 3)
        problemas.append(f"{len(mega_duras)} falas congeladas na tela (> 8s).")

    if cps_medio > 26.0:
        score -= 15
        problemas.append(f"Velocidade de leitura excessiva (média {cps_medio} CPS).")

    if cobertura_pct < 85.0:
        score -= 15
        problemas.append(f"Legenda termina antes do áudio ({cobertura_pct}% do tempo coberto).")

    # Amontoamento em massa (> 10) zera o score de integridade
    if len(amontoadas) >= 10:
        score = 0

    score = max(0, min(100, score))
    aprovado = (score >= 90 and len(amontoadas) == 0 and len(retrocessos) == 0 and len(micro_duras) <= 2 and len(alucinacoes) == 0)

    if aprovado:
        resumo = f"100% Íntegro ({total_cues} falas • 0 amontoadas • CPS: {cps_medio})"
    else:
        resumo = f"Score {score}%: {len(amontoadas)} amontoadas, {len(micro_duras)} micro, {len(mega_duras)} mega"

    return {
        'aprovado': aprovado,
        'score': score,
        'total_cues': total_cues,
        'amontoadas': len(amontoadas),
        'detalhes_amontoadas': amontoadas[:10],
        'micro_duras': len(micro_duras),
        'mega_duras': len(mega_duras),
        'retrocessos': len(retrocessos),
        'cps_medio': cps_medio,
        'cps_max': cps_max,
        'cobertura_audio_pct': cobertura_pct,
        'problemas': problemas,
        'resumo': resumo
    }

def reparar_srt(srt_conteudo, duracao_audio_seg=None):
    """
    Repara automaticamente qualquer arquivo SRT que possua falas amontoadas,
    micro-durações, colisões, palavras órfãs, cues vazios ou mega-durações congeladas.
    Retorna (novo_srt_texto, novo_relatorio_auditoria).
    """
    blocos = parse_srt(srt_conteudo)
    if not blocos:
        return srt_conteudo, auditar_srt(srt_conteudo, duracao_audio_seg)

    # 1. Normaliza gramática, elimina cues vazios e duplicatas exatas consecutivas
    limpos = []
    for b in blocos:
        txt = normalizar_gramatica_srt(limpar_todas_tags(b.get('texto', '')))
        if not txt or eh_alucinacao_whisper(txt):
            continue
        b['texto'] = txt
        if limpos:
            ant = limpos[-1]
            if b['texto'] == ant['texto'] and abs(b['t0'] - ant['t0']) < 0.1:
                continue
        limpos.append(b)
    blocos = limpos
    if not blocos:
        return "", auditar_srt("", duracao_audio_seg)

    # 1.1 Passada anti-órfã: funde palavras isoladas (<= 1 palavra com dur < 0.85s ou <= 2 palavras com dur < 0.65s) no bloco anterior
    blocos_sem_orfas = []
    for b in blocos:
        txt = b['texto']
        words = txt.split()
        dur = b['t1'] - b['t0']
        if len(words) <= 1 and dur < 0.85 and blocos_sem_orfas:
            blocos_sem_orfas[-1]['t1'] = b['t1']
            blocos_sem_orfas[-1]['texto'] = normalizar_gramatica_srt(f"{blocos_sem_orfas[-1]['texto']} {txt}")
        elif len(words) <= 2 and dur < 0.65 and blocos_sem_orfas:
            blocos_sem_orfas[-1]['t1'] = b['t1']
            blocos_sem_orfas[-1]['texto'] = normalizar_gramatica_srt(f"{blocos_sem_orfas[-1]['texto']} {txt}")
        else:
            blocos_sem_orfas.append(b)
    blocos = blocos_sem_orfas

    # 2. Localiza e redistribui clusters amontoados
    n = len(blocos)
    i = 0
    while i < n:
        j = i + 1
        while j < n and (blocos[j]['t0'] - blocos[j - 1]['t0']) < 0.25:
            j += 1

        tamanho_cluster = j - i
        if tamanho_cluster > 1:
            t_ini = blocos[i]['t0']
            if j < n:
                t_prox = blocos[j]['t0']
                t_fim = max(t_ini + tamanho_cluster * 0.8, t_prox - 0.05)
            else:
                if duracao_audio_seg and duracao_audio_seg > t_ini:
                    t_fim = duracao_audio_seg
                else:
                    t_fim = t_ini + sum(max(1.2, len(b['texto']) * 0.065) for b in blocos[i:j])

            total_dur = max(tamanho_cluster * 0.8, t_fim - t_ini)
            total_chars = sum(max(10, len(b['texto'])) for b in blocos[i:j])
            cur = t_ini
            for k in range(i, j):
                peso = max(10, len(blocos[k]['texto'])) / max(1, total_chars)
                dur_k = max(0.5, peso * (total_dur - (tamanho_cluster - 1) * 0.05))
                blocos[k]['t0'] = round(cur, 3)
                blocos[k]['t1'] = round(cur + dur_k, 3)
                cur = blocos[k]['t1'] + 0.05
            i = j
        else:
            i += 1

    # 3. Garante monotonicidade estrita e limites de leitura legível
    for idx in range(len(blocos)):
        b = blocos[idx]
        if idx > 0:
            b_ant = blocos[idx - 1]
            if b['t0'] < b_ant['t1']:
                b['t0'] = round(b_ant['t1'] + 0.05, 3)
        dur_min = max(0.5, min(7.5, len(b['texto']) * 0.04))
        if b['t1'] <= b['t0'] or (b['t1'] - b['t0']) < dur_min:
            b['t1'] = round(b['t0'] + dur_min, 3)
        elif (b['t1'] - b['t0']) > 8.0:
            dur_ideal = max(3.0, min(7.5, len(b['texto']) * 0.075))
            b['t1'] = round(b['t0'] + dur_ideal, 3)

    # 4. Formata novo SRT
    linhas = []
    for idx, b in enumerate(blocos, 1):
        linhas.append(str(idx))
        linhas.append(f"{formatar_tempo(b['t0'])} --> {formatar_tempo(b['t1'])}")
        linhas.append(b['texto'])
        linhas.append("")

    novo_srt = "\n".join(linhas).strip() + "\n"
    novo_relatorio = auditar_srt(novo_srt, duracao_audio_seg)
    return novo_srt, novo_relatorio

def gerar_srt_whisper(audio_path, texto_referencia="", max_caracteres=42, retornar_meta=False, progress_callback=None):
    """
    Função principal chamada pela ponte para gerar a legenda SRT completa.
    Gera blocos sincronizados, audita o resultado e garante 100% de integridade.
    """
    texto_referencia = limpar_todas_tags(texto_referencia)
    blocos, duracao, info_meta = _gerar_blocos_srt(audio_path, texto_referencia, max_caracteres=max_caracteres, progress_callback=progress_callback)
    srt_conteudo = _formatar_srt(blocos)

    # Auditoria de integridade matemática (sem ajuste automático silencioso)
    auditoria = auditar_srt(srt_conteudo, duracao_audio_seg=duracao)
    info_meta['auditoria'] = auditoria
    info_meta['similaridade'] = auditoria['score']
    if auditoria['aprovado'] and auditoria['amontoadas'] == 0:
        info_meta['aviso'] = f"✅ Legenda 100% íntegra ({auditoria['total_cues']} falas • 0 amontoadas)"
    else:
        info_meta['aviso'] = f"⚠️ Legenda gerada com {auditoria['amontoadas']} fala(s) amontoada(s) (Score: {auditoria['score']}%)"

    if progress_callback:
        progress_callback("Legenda SRT concluída!", pct=98)

    if retornar_meta:
        return srt_conteudo, info_meta
    return srt_conteudo

