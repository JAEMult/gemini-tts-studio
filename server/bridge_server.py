# -*- coding: utf-8 -*-
import os
import sys
import json
import base64
import time
import shutil
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import threading
import subprocess
from datetime import datetime

class SafeStream:
    def __init__(self, target):
        self.target = target
    def write(self, s):
        try:
            if self.target:
                self.target.write(s)
                self.target.flush()
        except Exception:
            pass
    def flush(self):
        try:
            if self.target:
                self.target.flush()
        except Exception:
            pass

sys.stdout = SafeStream(sys.stdout)
sys.stderr = SafeStream(sys.stderr)

# Garante que a pasta atual do server esteja no sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import pipe_runner

_transcribe_whisper = None
_transcribe_mtime = 0
def get_transcribe_whisper():
    global _transcribe_whisper, _transcribe_mtime
    tw_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'transcribe_whisper.py')
    current_mtime = os.path.getmtime(tw_path) if os.path.isfile(tw_path) else 0
    if _transcribe_whisper is None:
        import transcribe_whisper as tw
        _transcribe_whisper = tw
        _transcribe_mtime = current_mtime
    elif current_mtime > _transcribe_mtime:
        import importlib
        _transcribe_whisper = importlib.reload(_transcribe_whisper)
        _transcribe_mtime = current_mtime
    return _transcribe_whisper

# Pré-aquece o Whisper em segundo plano sem bloquear a inicialização instantânea da porta 5006
threading.Thread(target=get_transcribe_whisper, daemon=True).start()

PORT = 5006
PROGRESSO_ATUAL = "Pronto"
PROGRESSO_PCT = 0
JOBS_ATIVOS = {}
JOBS_LOCK = threading.Lock()

def set_progresso(msg, pct=None):
    global PROGRESSO_ATUAL, PROGRESSO_PCT
    PROGRESSO_ATUAL = msg
    if pct is not None:
        PROGRESSO_PCT = max(0, min(100, int(pct)))
    sys.stderr.write(f"[Status] ({PROGRESSO_PCT}%) {msg}\n")

def limpar_jobs_antigos(max_idade_horas=24):
    """
    Remove automaticamente pastas de jobs em 'Processados' que tenham mais de 24 horas (ou tempo customizado).
    Garante que arquivos intermediários não fiquem ocupando espaço no computador do usuário.
    """
    try:
        raiz_dev = os.path.abspath(os.path.join(BASE_DIR, '..'))
        pasta_proc = os.path.join(raiz_dev, 'Processados')
        if not os.path.isdir(pasta_proc):
            return 0
        agora = time.time()
        limite_seg = max_idade_horas * 3600
        removidos = 0
        for item in os.listdir(pasta_proc):
            caminho = os.path.join(pasta_proc, item)
            if os.path.isdir(caminho) and item.startswith('job_'):
                try:
                    idade = agora - os.path.getmtime(caminho)
                    if idade > limite_seg:
                        shutil.rmtree(caminho, ignore_errors=True)
                        removidos += 1
                        sys.stderr.write(f"[AutoClean] Job temporário expirado removido (> {max_idade_horas}h): {item}\n")
                except Exception:
                    pass
        return removidos
    except Exception as e:
        sys.stderr.write(f"[AutoClean] Erro na limpeza automática: {e}\n")
        return 0

def calcular_metadados_audio(caminho_wav):
    dur_seg = 0.0
    tam_bytes = 0
    for _ in range(6):
        try:
            if os.path.isfile(caminho_wav):
                tam_bytes = os.path.getsize(caminho_wav)
                if tam_bytes > 0:
                    import wave
                    with wave.open(caminho_wav, 'rb') as wf:
                        frames = wf.getnframes()
                        rate = wf.getframerate()
                        if rate > 0:
                            dur_seg = frames / float(rate)
                    if dur_seg > 0:
                        break
        except Exception:
            pass
        time.sleep(0.15)
    m = int(dur_seg // 60)
    s = int(dur_seg % 60)
    dur_fmt = f"{m:02d}:{s:02d}"
    if tam_bytes < 1024 * 1024:
        tam_fmt = f"{tam_bytes / 1024:.1f} KB"
    else:
        tam_fmt = f"{tam_bytes / (1024 * 1024):.2f} MB"
    return {
        'duracao_seg': round(dur_seg, 2),
        'duracao_fmt': dur_fmt,
        'tamanho_bytes': tam_bytes,
        'tamanho_fmt': tam_fmt
    }

def concatenar_wavs(itens, caminho_saida):
    """Concatena arquivos de áudio preservando integridade, usando módulo wave para WAVs idênticos ou ffmpeg como fallback universal."""
    caminhos = []
    for it in itens:
        if isinstance(it, dict):
            c = it.get('caminho_wav') or it.get('caminho')
        else:
            c = str(it)
        if c and os.path.isfile(c):
            caminhos.append(c)

    if not caminhos:
        return

    if len(caminhos) == 1:
        shutil.copy2(caminhos[0], caminho_saida)
        return

    # Tenta concatenação rápida via módulo wave nativo se todos forem WAVs de parâmetros idênticos
    todos_wav = all(c.lower().endswith('.wav') for c in caminhos)
    if todos_wav:
        try:
            with wave.open(caminhos[0], 'rb') as w_first:
                params = w_first.getparams()
            compativeis = True
            for c in caminhos[1:]:
                with wave.open(c, 'rb') as w_chk:
                    if (w_chk.getnchannels() != params.nchannels or
                        w_chk.getsampwidth() != params.sampwidth or
                        w_chk.getframerate() != params.framerate):
                        compativeis = False
                        break
            if compativeis:
                with wave.open(caminho_saida, 'wb') as w_out:
                    w_out.setparams(params)
                    for c in caminhos:
                        with wave.open(c, 'rb') as w_in:
                            w_out.writeframes(w_in.readframes(w_in.getnframes()))
                return
        except Exception:
            pass

    # Fallback universal via ffmpeg sem popup de janela de console
    ffmpeg_exe = shutil.which('ffmpeg')
    if ffmpeg_exe:
        try:
            list_txt = caminho_saida + '.concat.txt'
            with open(list_txt, 'w', encoding='utf-8') as f:
                for c in caminhos:
                    f.write(f"file '{os.path.abspath(c).replace(chr(92), '/')}'\n")
            CREATE_NO_WINDOW = 0x08000000
            cmd = [ffmpeg_exe, '-y', '-f', 'concat', '-safe', '0', '-i', list_txt, '-c:a', 'pcm_s16le', caminho_saida]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
            try:
                os.remove(list_txt)
            except Exception:
                pass
            if res.returncode == 0 and os.path.isfile(caminho_saida) and os.path.getsize(caminho_saida) > 100:
                return
        except Exception:
            pass

    # Fallback final de emergência
    shutil.copy2(caminhos[0], caminho_saida)


class HistoricoSilencioManager:
    """
    Gerencia o histórico de cortes de silêncio e versões de áudio/legenda (.wav e .srt)
    dentro da pasta oculta .historico no diretório de saída do job.
    Permite rollback instantâneo para qualquer versão anterior (Original, corte 1, corte 2, etc.).
    """
    @staticmethod
    def _pasta_hist(caminho_wav):
        pasta_saida = os.path.dirname(os.path.abspath(caminho_wav))
        pasta_hist = os.path.join(pasta_saida, '.historico')
        os.makedirs(pasta_hist, exist_ok=True)
        return pasta_hist

    @staticmethod
    def _meta_path(caminho_wav):
        return os.path.join(HistoricoSilencioManager._pasta_hist(caminho_wav), 'meta_historico.json')

    @staticmethod
    def _carregar_meta(caminho_wav):
        p = HistoricoSilencioManager._meta_path(caminho_wav)
        if os.path.isfile(p):
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    @staticmethod
    def _salvar_meta(caminho_wav, dados):
        p = HistoricoSilencioManager._meta_path(caminho_wav)
        try:
            with open(p, 'w', encoding='utf-8') as f:
                json.dump(dados, f, ensure_ascii=False, indent=2)
        except Exception as e:
            sys.stderr.write(f"[HistoricoSilencio] Erro ao salvar meta: {e}\n")

    @classmethod
    def registrar_original_se_necessario(cls, caminho_wav, caminho_srt=""):
        if not os.path.isfile(caminho_wav):
            return None
        nome_arq = os.path.basename(caminho_wav)
        meta_dados = cls._carregar_meta(caminho_wav)
        if nome_arq in meta_dados and meta_dados[nome_arq].get('versoes'):
            return meta_dados[nome_arq]['versoes'][0]

        pasta_hist = cls._pasta_hist(caminho_wav)
        nome_base = os.path.splitext(nome_arq)[0]
        wav_orig = os.path.join(pasta_hist, f"{nome_base}_v0_Original.wav")
        try:
            shutil.copy2(caminho_wav, wav_orig)
        except Exception as e:
            sys.stderr.write(f"[HistoricoSilencio] Erro ao copiar original: {e}\n")
            return None

        srt_orig = ""
        caminho_srt_cand = caminho_srt if (caminho_srt and os.path.isfile(caminho_srt)) else (os.path.splitext(caminho_wav)[0] + '.srt')
        if os.path.isfile(caminho_srt_cand):
            srt_orig = os.path.join(pasta_hist, f"{nome_base}_v0_Original.srt")
            try:
                shutil.copy2(caminho_srt_cand, srt_orig)
            except Exception:
                pass

        meta_audio = calcular_metadados_audio(caminho_wav)
        v0 = {
            'id': 0,
            'label': 'Original',
            'preset': 'Original',
            'timestamp': int(time.time()),
            'dataFmt': datetime.now().strftime('%d/%m %H:%M'),
            'duracaoSeg': meta_audio['duracao_seg'],
            'duracaoFmt': meta_audio['duracao_fmt'],
            'tamanhoFmt': meta_audio['tamanho_fmt'],
            'tamanhoBytes': meta_audio['tamanho_bytes'],
            'reducaoSeg': 0.0,
            'reducaoFmt': '',
            'caminhoWav': wav_orig,
            'caminhoSrt': srt_orig
        }
        meta_dados[nome_arq] = {
            'arquivo': nome_arq,
            'versaoAtual': 0,
            'versoes': [v0]
        }
        cls._salvar_meta(caminho_wav, meta_dados)
        return v0

    @classmethod
    def registrar_novo_corte(cls, caminho_wav, preset, duracao_antiga_seg, caminho_srt=""):
        if not os.path.isfile(caminho_wav):
            return None, []
        nome_arq = os.path.basename(caminho_wav)
        cls.registrar_original_se_necessario(caminho_wav, caminho_srt)

        meta_dados = cls._carregar_meta(caminho_wav)
        entry = meta_dados.get(nome_arq, {'versoes': [], 'versaoAtual': 0})
        nova_id = len(entry['versoes'])

        pasta_hist = cls._pasta_hist(caminho_wav)
        nome_base = os.path.splitext(nome_arq)[0]
        wav_ver = os.path.join(pasta_hist, f"{nome_base}_v{nova_id}_{preset}.wav")
        try:
            shutil.copy2(caminho_wav, wav_ver)
        except Exception as e:
            sys.stderr.write(f"[HistoricoSilencio] Erro ao salvar versão {nova_id}: {e}\n")

        srt_ver = ""
        caminho_srt_cand = caminho_srt if (caminho_srt and os.path.isfile(caminho_srt)) else (os.path.splitext(caminho_wav)[0] + '.srt')
        if os.path.isfile(caminho_srt_cand):
            srt_ver = os.path.join(pasta_hist, f"{nome_base}_v{nova_id}_{preset}.srt")
            try:
                shutil.copy2(caminho_srt_cand, srt_ver)
            except Exception:
                pass

        meta_novo = calcular_metadados_audio(caminho_wav)
        reducao_seg = max(0.0, round(duracao_antiga_seg - meta_novo['duracao_seg'], 1))
        pct = round((reducao_seg / duracao_antiga_seg) * 100) if duracao_antiga_seg > 0 else 0
        reducao_fmt = f"-{reducao_seg:.1f}s (-{pct}%)" if reducao_seg > 0 else "0s"

        label_preset = '2,0s / 50%' if '2' in preset else ('1,3s / 60%' if '1.3' in preset or '1,3' in preset else ('0,5s / 80%' if '0.5' in preset or '0,5' in preset else preset))

        nova_v = {
            'id': nova_id,
            'label': label_preset,
            'preset': preset,
            'timestamp': int(time.time()),
            'dataFmt': datetime.now().strftime('%d/%m %H:%M'),
            'duracaoSeg': meta_novo['duracao_seg'],
            'duracaoFmt': meta_novo['duracao_fmt'],
            'tamanhoFmt': meta_novo['tamanho_fmt'],
            'tamanhoBytes': meta_novo['tamanho_bytes'],
            'reducaoSeg': reducao_seg,
            'reducaoFmt': reducao_fmt,
            'caminhoWav': wav_ver,
            'caminhoSrt': srt_ver
        }
        entry['versoes'].append(nova_v)
        entry['versaoAtual'] = nova_id
        meta_dados[nome_arq] = entry
        cls._salvar_meta(caminho_wav, meta_dados)

        hist_lista = [dict(v, isAtual=(v['id'] == nova_id)) for v in entry['versoes']]
        return nova_v, hist_lista

    @classmethod
    def obter_historico(cls, caminho_wav):
        nome_arq = os.path.basename(caminho_wav)
        meta_dados = cls._carregar_meta(caminho_wav)
        entry = meta_dados.get(nome_arq)
        if not entry or not entry.get('versoes'):
            return []
        v_atual = entry.get('versaoAtual', len(entry['versoes']) - 1)
        return [dict(v, isAtual=(v['id'] == v_atual)) for v in entry['versoes']]

    @classmethod
    def restaurar_versao(cls, caminho_wav, versao_id):
        nome_arq = os.path.basename(caminho_wav)
        meta_dados = cls._carregar_meta(caminho_wav)
        entry = meta_dados.get(nome_arq)
        if not entry or not entry.get('versoes'):
            raise Exception("Nenhum histórico encontrado para este arquivo.")

        target = None
        for v in entry['versoes']:
            if v['id'] == versao_id:
                target = v
                break
        if not target:
            raise Exception(f"Versão {versao_id} não encontrada no histórico.")

        if not os.path.isfile(target['caminhoWav']):
            raise Exception(f"Arquivo da versão {versao_id} não encontrado no disco.")

        # Copia de volta o WAV
        shutil.copy2(target['caminhoWav'], caminho_wav)

        # Copia de volta o SRT se existir
        caminho_srt_dest = os.path.splitext(caminho_wav)[0] + '.srt'
        if target.get('caminhoSrt') and os.path.isfile(target['caminhoSrt']):
            shutil.copy2(target['caminhoSrt'], caminho_srt_dest)
        elif os.path.isfile(caminho_srt_dest) and not target.get('caminhoSrt'):
            try:
                os.remove(caminho_srt_dest)
            except Exception:
                pass

        entry['versaoAtual'] = versao_id
        meta_dados[nome_arq] = entry
        cls._salvar_meta(caminho_wav, meta_dados)

        meta_audio = calcular_metadados_audio(caminho_wav)
        srt_conteudo = ""
        if os.path.isfile(caminho_srt_dest):
            try:
                with open(caminho_srt_dest, 'r', encoding='utf-8', errors='ignore') as sf:
                    srt_conteudo = sf.read()
            except Exception:
                pass

        hist_lista = [dict(v, isAtual=(v['id'] == versao_id)) for v in entry['versoes']]
        return {
            'caminhoWav': caminho_wav,
            'duracaoFmt': meta_audio['duracao_fmt'],
            'duracaoSeg': meta_audio['duracao_seg'],
            'tamanhoFmt': meta_audio['tamanho_fmt'],
            'tamanhoBytes': meta_audio['tamanho_bytes'],
            'reducaoFmt': target.get('reducaoFmt', ''),
            'caminhoSrt': caminho_srt_dest if srt_conteudo else '',
            'srtConteudo': srt_conteudo,
            'historico': hist_lista
        }

def executar_pipeline_job(itens_processar, roteiro_completo, juntar, nome_macro, gerar_srt, pasta_job, pasta_temp, pasta_saida):
    """
    Pipeline unificado de automação no Audacity e Whisper.
    Executa os efeitos/junção no Audacity e em seguida gera as legendas via Whisper (GPU CUDA).
    Utilizado tanto pelo upload em streaming quanto pela rota legada.
    """
    # 1. Executa Audacity (se não for apenas geração isolada de legendas SRT)
    if nome_macro == 'apenas_srt':
        set_progresso("Iniciando transcrição das legendas (.srt) diretamente no Whisper (GPU)...", pct=10)
        arquivos_audacity = []
        if juntar and len(itens_processar) > 1:
            nome_final = (itens_processar[0].get('nome_unificado') or 'audio_completo').replace('.wav', '').strip()
            caminho_dest = os.path.join(pasta_saida, f'{nome_final}.wav')
            try:
                concatenar_wavs(itens_processar, caminho_dest)
            except Exception as e_cat:
                sys.stderr.write(f"[apenas_srt] Erro concatenando WAVs: {e_cat}. Copiando primeiro arquivo.\n")
                shutil.copy2(itens_processar[0]['caminho_wav'], caminho_dest)
            arquivos_audacity.append({
                'tipo': 'unificado',
                'nome': nome_final,
                'caminho': caminho_dest,
                'tamanho': os.path.getsize(caminho_dest)
            })
        elif juntar and len(itens_processar) == 1:
            nome_final = (itens_processar[0].get('nome_unificado') or itens_processar[0].get('nome', 'audio_completo')).replace('.wav', '').strip()
            caminho_dest = os.path.join(pasta_saida, f'{nome_final}.wav')
            shutil.copy2(itens_processar[0]['caminho_wav'], caminho_dest)
            arquivos_audacity.append({
                'tipo': 'unificado',
                'nome': nome_final,
                'caminho': caminho_dest,
                'tamanho': os.path.getsize(caminho_dest)
            })
        else:
            for idx, item in enumerate(itens_processar, 1):
                nome = item.get('nome', f'bloco_{idx}').replace('.wav', '').strip()
                ext = os.path.splitext(item['caminho_wav'])[1].lower() or '.wav'
                caminho_dest = os.path.join(pasta_saida, f'{nome}{ext}')
                shutil.copy2(item['caminho_wav'], caminho_dest)
                arquivos_audacity.append({
                    'tipo': 'bloco',
                    'nome': nome,
                    'caminho': caminho_dest,
                    'tamanho': os.path.getsize(caminho_dest)
                })
    else:
        # Acha caminho da macro
        macro_path = None
        if nome_macro and nome_macro != 'none':
            for m in pipe_runner.list_available_macros():
                if m['arquivo'] == nome_macro or m['nome'] == nome_macro:
                    macro_path = m['caminho']
                    break

        res_audacity = pipe_runner.executar_processamento_audacity(
            itens_processar,
            juntar=juntar,
            macro_path=macro_path,
            pasta_saida=pasta_saida,
            progress_callback=set_progresso
        )

        if not res_audacity.get('success'):
            set_progresso("Falha no Audacity.")
            return res_audacity

        arquivos_audacity = res_audacity.get('arquivos', [])

    # 2. Executa Whisper se solicitado (Última coisa a ser gerada antes da entrega)
    itens_finais = []

    if juntar:
        # 1 áudio único -> 1 SRT único como última etapa
        audio_final = arquivos_audacity[0]
        texto_ref = " ".join(roteiro_completo)
        srt_conteudo = ""
        caminho_srt = ""
        aviso_srt = ""
        similaridade_srt = 100

        if gerar_srt:
            set_progresso("Whisper transcrevendo áudio unificado na GPU (última etapa)...", pct=45)
            try:
                srt_conteudo, info_meta = get_transcribe_whisper().gerar_srt_whisper(
                    audio_final['caminho'],
                    texto_referencia=texto_ref,
                    retornar_meta=True
                )
                caminho_srt = os.path.join(pasta_saida, f"{audio_final['nome']}.srt")
                with open(caminho_srt, 'w', encoding='utf-8') as sf:
                    sf.write(srt_conteudo)
                aviso_srt = info_meta.get('aviso', '')
                similaridade_srt = info_meta.get('similaridade', 100)
                set_progresso("Legenda unificada gerada com sucesso!", pct=95)
            except Exception as ew:
                sys.stderr.write(f"[Whisper] Erro na transcrição: {ew}\n")

        meta = calcular_metadados_audio(audio_final['caminho'])
        HistoricoSilencioManager.registrar_original_se_necessario(audio_final['caminho'], caminho_srt)
        hist_init = HistoricoSilencioManager.obter_historico(audio_final['caminho'])

        itens_finais.append({
            'tipo': 'unificado',
            'nome': audio_final['nome'],
            'macro': nome_macro,
            'texto': texto_ref,
            'caminhoWav': audio_final['caminho'],
            'urlAudio': f'/api/audio?path={urllib.parse.quote(audio_final["caminho"])}',
            'urlWav': f'/api/download?path={urllib.parse.quote(audio_final["caminho"])}',
            'tamanhoFmt': meta['tamanho_fmt'],
            'tamanhoBytes': meta['tamanho_bytes'],
            'duracaoFmt': meta['duracao_fmt'],
            'duracaoSeg': meta['duracao_seg'],
            'caminhoSrt': caminho_srt,
            'urlSrt': f'/api/download?path={urllib.parse.quote(caminho_srt)}' if caminho_srt else '',
            'srtConteudo': srt_conteudo,
            'avisoSrt': aviso_srt,
            'similaridadeSrt': similaridade_srt,
            'historico': hist_init
        })

    else:
        # Bloco a bloco: Whisper gera o SRT de cada um após a exportação
        total_bl = len(arquivos_audacity)
        for idx, arq in enumerate(arquivos_audacity):
            texto_ref = itens_processar[idx]['texto'] if idx < len(itens_processar) else ""
            srt_conteudo = ""
            caminho_srt = ""
            aviso_srt = ""
            similaridade_srt = 100

            if gerar_srt:
                pct_inicio = 20 + int((idx / total_bl) * 75)
                set_progresso(f"Whisper transcrevendo bloco {idx+1}/{total_bl} na GPU...", pct=pct_inicio)
                try:
                    srt_conteudo, info_meta = get_transcribe_whisper().gerar_srt_whisper(
                        arq['caminho'],
                        texto_referencia=texto_ref,
                        retornar_meta=True
                    )
                    caminho_srt = os.path.join(pasta_saida, f"{arq['nome']}.srt")
                    with open(caminho_srt, 'w', encoding='utf-8') as sf:
                        sf.write(srt_conteudo)
                    aviso_srt = info_meta.get('aviso', '')
                    similaridade_srt = info_meta.get('similaridade', 100)
                    pct_fim = 20 + int(((idx + 1) / total_bl) * 75)
                    set_progresso(f"Bloco {idx+1}/{total_bl} concluído!", pct=pct_fim)
                except Exception as ew:
                    sys.stderr.write(f"[Whisper] Erro no bloco {idx+1}: {ew}\n")

            meta = calcular_metadados_audio(arq['caminho'])
            HistoricoSilencioManager.registrar_original_se_necessario(arq['caminho'], caminho_srt)
            hist_init = HistoricoSilencioManager.obter_historico(arq['caminho'])

            itens_finais.append({
                'tipo': arq.get('tipo', 'bloco'),
                'nome': arq['nome'],
                'macro': nome_macro,
                'texto': texto_ref,
                'caminhoWav': arq['caminho'],
                'urlAudio': f'/api/audio?path={urllib.parse.quote(arq["caminho"])}',
                'urlWav': f'/api/download?path={urllib.parse.quote(arq["caminho"])}',
                'tamanhoFmt': meta['tamanho_fmt'],
                'tamanhoBytes': meta['tamanho_bytes'],
                'duracaoFmt': meta['duracao_fmt'],
                'duracaoSeg': meta['duracao_seg'],
                'caminhoSrt': caminho_srt,
                'urlSrt': f'/api/download?path={urllib.parse.quote(caminho_srt)}' if caminho_srt else '',
                'srtConteudo': srt_conteudo,
                'avisoSrt': aviso_srt,
                'similaridadeSrt': similaridade_srt,
                'historico': hist_init
            })

    # Limpa arquivos temporários de entrada
    shutil.rmtree(pasta_temp, ignore_errors=True)

    set_progresso("Processamento concluído com sucesso!", pct=100)
    return {
        'success': True,
        'juntar': juntar,
        'pastaJob': pasta_job,
        'itens': itens_finais
    }


# Payload comprimido em base64 do som Liecio Minimalista (garante portabilidade em qualquer PC)
SOM_SILENCIO_ZLIB_B64 = "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"

def tocar_efeito_silencio_local():
    try:
        import winsound
        wav_efeito = r"C:\Projetos\Efeitos Sonoros\liecio-menu_beep_short_snap-533778 Minimalista.wav"
        if os.path.isfile(wav_efeito):
            winsound.PlaySound(wav_efeito, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return
    except Exception:
        pass
    try:
        import winsound, zlib, base64
        wav_raw = zlib.decompress(base64.b64decode(SOM_SILENCIO_ZLIB_B64))
        winsound.PlaySound(wav_raw, winsound.SND_MEMORY | winsound.SND_ASYNC)
    except Exception:
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass

class BridgeHandler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def log_message(self, format, *args):
        # Silencia erros de stream quebrado caso o processo rode em segundo plano sem console ativo
        try:
            if sys.stderr:
                sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), format % args))
        except Exception:
            pass

    def responder_json(self, dados, status=200):
        corpo = json.dumps(dados, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(corpo)))
        self.send_header('Connection', 'close')
        self.end_headers()
        try:
            self.wfile.write(corpo)
            self.wfile.flush()
        except Exception:
            pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        caminho = parsed.path

        if caminho == '/api/status':
            macros = pipe_runner.list_available_macros()
            audacity_exe = pipe_runner.find_audacity_exe()
            self.responder_json({
                'ok': True,
                'audacityInstalado': audacity_exe is not None,
                'audacityCaminho': audacity_exe or '',
                'audacityRunning': pipe_runner.is_audacity_running(),
                'macros': macros,
                'whisperPronto': True,
                'progresso': PROGRESSO_ATUAL
            })

        elif caminho == '/api/progresso':
            self.responder_json({
                'progresso': PROGRESSO_ATUAL,
                'pct': PROGRESSO_PCT
            })

        elif caminho == '/api/tocar-concluido':
            def _tocar():
                try:
                    import winsound
                    # 1. Som de asterisco do Windows
                    winsound.MessageBeep(winsound.MB_ICONASTERISK)
                    time.sleep(0.05)
                    # 2. Beep melódico de vitória (C5, E5, G5, C6)
                    winsound.Beep(523, 100)
                    winsound.Beep(659, 100)
                    winsound.Beep(784, 120)
                    winsound.Beep(1046, 250)
                except Exception:
                    pass
            threading.Thread(target=_tocar, daemon=True).start()
            self.responder_json({'success': True})

        elif caminho == '/api/tocar-som-silencio':
            threading.Thread(target=tocar_efeito_silencio_local, daemon=True).start()
            self.responder_json({'success': True})

        elif caminho == '/api/audio':
            # Rota de streaming direto para o player do navegador (inline, sem attachment)
            query = urllib.parse.parse_qs(parsed.query)
            caminho_arquivo = query.get('path', [''])[0]
            if os.path.isfile(caminho_arquivo):
                ext = os.path.splitext(caminho_arquivo)[1].lower()
                mime = 'audio/wav' if ext == '.wav' else 'audio/mpeg'
                tamanho = os.path.getsize(caminho_arquivo)

                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(tamanho))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Disposition', 'inline')
                self.end_headers()

                with open(caminho_arquivo, 'rb') as f:
                    shutil.copyfileobj(f, self.wfile)
            else:
                self.responder_json({'erro': 'Arquivo de áudio não encontrado'}, status=404)

        elif caminho == '/api/app/fechar':
            pipe_runner.close_audacity(force=True)
            self.responder_json({'ok': True, 'msg': 'Audacity encerrado com sucesso.'})

        elif caminho == '/api/audacity/historico':
            query = urllib.parse.parse_qs(parsed.query)
            caminho_arquivo = query.get('path', [''])[0]
            if os.path.isfile(caminho_arquivo):
                hist = HistoricoSilencioManager.obter_historico(caminho_arquivo)
                self.responder_json({'success': True, 'historico': hist})
            else:
                self.responder_json({'success': False, 'error': 'Arquivo não encontrado'}, status=404)

        elif caminho == '/api/download':
            query = urllib.parse.parse_qs(parsed.query)
            caminho_arquivo = query.get('path', [''])[0]
            if os.path.isfile(caminho_arquivo):
                ext = os.path.splitext(caminho_arquivo)[1].lower()
                mime = 'audio/wav' if ext == '.wav' else 'text/plain; charset=utf-8'
                nome_download = os.path.basename(caminho_arquivo)
                tamanho = os.path.getsize(caminho_arquivo)

                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(tamanho))
                self.send_header('Content-Disposition', f'attachment; filename="{nome_download}"')
                self.end_headers()

                with open(caminho_arquivo, 'rb') as f:
                    shutil.copyfileobj(f, self.wfile)
            else:
                self.responder_json({'erro': 'Arquivo não encontrado'}, status=404)

        else:
            self.responder_json({'erro': 'Rota não encontrada'}, status=404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        caminho = parsed.path

        if caminho == '/api/audacity/fechar':
            pipe_runner.close_audacity(force=True)
            self.responder_json({'ok': True, 'msg': 'Audacity encerrado com sucesso.'})

        elif caminho == '/api/app/fechar':
            pipe_runner.close_audacity(force=True)
            self.responder_json({'ok': True, 'msg': 'Audacity encerrado com sucesso.'})

        elif caminho == '/api/tocar-concluido':
            def _tocar_windows():
                try:
                    import winsound
                    winsound.MessageBeep(winsound.MB_ICONASTERISK)
                    winsound.Beep(523, 100)
                    winsound.Beep(659, 100)
                    winsound.Beep(784, 120)
                    winsound.Beep(1046, 250)
                except Exception as e:
                    sys.stderr.write(f"[Som] Erro ao emitir som: {e}\n")
            threading.Thread(target=_tocar_windows, daemon=True).start()
            self.responder_json({'success': True})

        elif caminho == '/api/limpar-cache':
            try:
                tamanho = int(self.headers.get('Content-Length', 0)) if self.headers.get('Content-Length') else 0
                dados = json.loads(self.rfile.read(tamanho).decode('utf-8')) if tamanho > 0 else {}
                limpar_tudo = bool(dados.get('tudo', False))
                horas = 0 if limpar_tudo else float(dados.get('horas', 24))
                removidos = limpar_jobs_antigos(max_idade_horas=horas)
                self.responder_json({'success': True, 'removidos': removidos})
            except Exception as e:
                self.responder_json({'success': False, 'error': str(e)}, status=500)

        elif caminho == '/api/whisper/gerar-srt':
            try:
                tamanho = int(self.headers.get('Content-Length', 0))
                corpo_bruto = self.rfile.read(tamanho)
                dados = json.loads(corpo_bruto.decode('utf-8'))
                caminho_wav = dados.get('caminhoWav', '')
                texto_ref = dados.get('textoReferencia', '')

                if not os.path.isfile(caminho_wav):
                    return self.responder_json({'success': False, 'error': f'Arquivo WAV não encontrado: {caminho_wav}'}, status=404)

                set_progresso("Whisper transcrevendo áudio na GPU sob demanda...")
                srt_conteudo, info_meta = get_transcribe_whisper().gerar_srt_whisper(
                    caminho_wav,
                    texto_referencia=texto_ref,
                    retornar_meta=True
                )
                caminho_srt = os.path.splitext(caminho_wav)[0] + '.srt'
                with open(caminho_srt, 'w', encoding='utf-8') as sf:
                    sf.write(srt_conteudo)

                set_progresso("Legenda SRT gerada com sucesso!")
                self.responder_json({
                    'success': True,
                    'caminhoSrt': caminho_srt,
                    'urlSrt': f'/api/download?path={urllib.parse.quote(caminho_srt)}',
                    'srtConteudo': srt_conteudo,
                    'aviso': info_meta.get('aviso', ''),
                    'similaridade': info_meta.get('similaridade', 100)
                })
            except Exception as e:
                set_progresso(f"Erro Whisper: {e}")
                self.responder_json({'success': False, 'error': str(e)}, status=500)

        elif caminho == '/api/tocar-concluido':
            def _tocar():
                try:
                    import winsound
                    winsound.MessageBeep(winsound.MB_ICONASTERISK)
                    time.sleep(0.05)
                    winsound.Beep(523, 100)
                    winsound.Beep(659, 100)
                    winsound.Beep(784, 120)
                    winsound.Beep(1046, 250)
                except Exception:
                    pass
            threading.Thread(target=_tocar, daemon=True).start()
            self.responder_json({'success': True})

        elif caminho == '/api/tocar-som-silencio':
            threading.Thread(target=tocar_efeito_silencio_local, daemon=True).start()
            self.responder_json({'success': True})

        elif caminho == '/api/audacity/travar-silencio':
            try:
                tamanho = int(self.headers.get('Content-Length', 0))
                corpo_bruto = self.rfile.read(tamanho)
                dados = json.loads(corpo_bruto.decode('utf-8'))

                caminho_wav = dados.get('caminhoWav', '')
                texto_ref = dados.get('textoReferencia', '')
                gerar_srt = bool(dados.get('gerarSrt', True))

                # Suporte a presets e configurações dinâmicas
                preset = dados.get('preset', '2.0_50')
                if preset in ('2s_50', '2.0_50', '2_50', '2s_40', '2.0_40', '2_40'):
                    duracao, compressao = '2', '50'
                elif preset in ('1.3s_60', '1.3_60', '1.3s_30', '1.3_30'):
                    duracao, compressao = '1,3', '60'
                elif preset in ('0.5s_80', '0.5_80', '0.5s_60', '0.5_60'):
                    duracao, compressao = '0,5', '80'
                else:
                    duracao = str(dados.get('duracao', '2'))
                    compressao = str(dados.get('compressao', '50'))

                limiar = str(dados.get('limiar', '-35'))
                descartar = str(dados.get('descartar', '0,5'))

                if not os.path.isfile(caminho_wav):
                    return self.responder_json({'success': False, 'error': f'Arquivo WAV não encontrado: {caminho_wav}'}, status=404)

                caminho_srt_existente = os.path.splitext(caminho_wav)[0] + '.srt'
                meta_antes = calcular_metadados_audio(caminho_wav)
                duracao_antiga = meta_antes['duracao_seg']
                HistoricoSilencioManager.registrar_original_se_necessario(caminho_wav, caminho_srt_existente)

                set_progresso(f"Aplicando corte de silêncio ({duracao}s / {compressao}%) no Audacity: {os.path.basename(caminho_wav)}...", pct=10)
                res_audacity = pipe_runner.executar_travar_silencio(
                    caminho_wav,
                    duracao=duracao,
                    compressao=compressao,
                    limiar=limiar,
                    descartar=descartar,
                    progress_callback=set_progresso
                )

                if not res_audacity.get('success'):
                    set_progresso("Falha ao travar silêncio no Audacity.", pct=0)
                    return self.responder_json(res_audacity, status=500)

                # Recalcula metadados de áudio após redução de silêncio
                meta = calcular_metadados_audio(caminho_wav)

                # Preserva o arquivo .srt existente se houver (sem travar a requisição com Whisper)
                caminho_srt = caminho_srt_existente if os.path.isfile(caminho_srt_existente) else ""
                srt_conteudo = ""
                if caminho_srt:
                    try:
                        with open(caminho_srt, 'r', encoding='utf-8') as sf:
                            srt_conteudo = sf.read()
                    except Exception:
                        pass
                aviso_srt = ""

                nova_v, hist_lista = HistoricoSilencioManager.registrar_novo_corte(
                    caminho_wav, preset, duracao_antiga, caminho_srt=caminho_srt
                )

                set_progresso("Silêncio travado e áudio atualizado com sucesso!", pct=100)
                ts_cache = int(time.time())
                self.responder_json({
                    'success': True,
                    'caminhoWav': caminho_wav,
                    'tamanhoFmt': meta['tamanho_fmt'],
                    'tamanhoBytes': meta['tamanho_bytes'],
                    'duracaoFmt': meta['duracao_fmt'],
                    'duracaoSeg': meta['duracao_seg'],
                    'reducaoSeg': nova_v['reducaoSeg'] if nova_v else 0.0,
                    'reducaoFmt': nova_v['reducaoFmt'] if nova_v else '',
                    'historico': hist_lista,
                    'urlAudio': f'/api/audio?path={urllib.parse.quote(caminho_wav)}&t={ts_cache}',
                    'urlWav': f'/api/download?path={urllib.parse.quote(caminho_wav)}&t={ts_cache}',
                    'caminhoSrt': caminho_srt,
                    'urlSrt': f'/api/download?path={urllib.parse.quote(caminho_srt)}&t={ts_cache}' if caminho_srt else '',
                    'srtConteudo': srt_conteudo,
                    'aviso': aviso_srt
                })
            except Exception as e:
                set_progresso(f"Erro ao travar silêncio: {e}")
                self.responder_json({'success': False, 'error': str(e)}, status=500)

        elif caminho == '/api/audacity/travar-silencio-lote':
            try:
                tamanho = int(self.headers.get('Content-Length', 0))
                corpo_bruto = self.rfile.read(tamanho)
                dados = json.loads(corpo_bruto.decode('utf-8'))

                itens = dados.get('itens', [])
                if not itens:
                    return self.responder_json({'success': False, 'error': 'Nenhum áudio informado.'}, status=400)

                preset = dados.get('preset', '2.0_50')
                if preset in ('2s_50', '2.0_50', '2_50', '2s_40', '2.0_40', '2_40'):
                    duracao, compressao = '2', '50'
                elif preset in ('1.3s_60', '1.3_60', '1.3s_30', '1.3_30'):
                    duracao, compressao = '1,3', '60'
                elif preset in ('0.5s_80', '0.5_80', '0.5s_60', '0.5_60'):
                    duracao, compressao = '0,5', '80'
                else:
                    duracao = str(dados.get('duracao', '2'))
                    compressao = str(dados.get('compressao', '50'))

                limiar = str(dados.get('limiar', '-35'))
                descartar = str(dados.get('descartar', '0,5'))

                # Registra originais e armazena durações pré-corte
                duracoes_antigas = {}
                for it in itens:
                    cw = it.get('caminhoWav') or it.get('caminho') or ''
                    if os.path.isfile(cw):
                        meta_it = calcular_metadados_audio(cw)
                        duracoes_antigas[cw] = meta_it['duracao_seg']
                        caminho_srt_cand = os.path.splitext(cw)[0] + '.srt'
                        HistoricoSilencioManager.registrar_original_se_necessario(cw, caminho_srt_cand)

                set_progresso(f"Iniciando corte de silêncio em lote no Audacity ({len(itens)} áudio(s))...", pct=5)
                res_lote = pipe_runner.executar_travar_silencio_lote(
                    itens,
                    duracao=duracao,
                    compressao=compressao,
                    limiar=limiar,
                    descartar=descartar,
                    progress_callback=set_progresso
                )

                if not res_lote.get('success'):
                    set_progresso("Falha no corte de silêncio em lote no Audacity.", pct=0)
                    return self.responder_json(res_lote, status=500)

                itens_processados = res_lote.get('itens', [])
                itens_atualizados = []
                total = len(itens_processados)

                for idx, it in enumerate(itens_processados, 1):
                    if not it.get('success'):
                        itens_atualizados.append(it)
                        continue

                    caminho_wav = it['caminhoWav']
                    meta = calcular_metadados_audio(caminho_wav)
                    caminho_srt_existente = os.path.splitext(caminho_wav)[0] + '.srt'
                    caminho_srt = caminho_srt_existente if os.path.isfile(caminho_srt_existente) else ""
                    srt_conteudo = ""
                    if caminho_srt:
                        try:
                            with open(caminho_srt, 'r', encoding='utf-8') as sf:
                                srt_conteudo = sf.read()
                        except Exception:
                            pass
                    aviso_srt = ""
                    similaridade_srt = 100

                    nova_v, hist_lista = HistoricoSilencioManager.registrar_novo_corte(
                        caminho_wav, preset, duracoes_antigas.get(caminho_wav, meta['duracao_seg']), caminho_srt=caminho_srt
                    )

                    ts_cache = int(time.time())
                    itens_atualizados.append({
                        'success': True,
                        'caminhoWav': caminho_wav,
                        'nome': it.get('nome', os.path.basename(caminho_wav)),
                        'tamanhoFmt': meta['tamanho_fmt'],
                        'tamanhoBytes': meta['tamanho_bytes'],
                        'duracaoFmt': meta['duracao_fmt'],
                        'duracaoSeg': meta['duracao_seg'],
                        'reducaoSeg': nova_v['reducaoSeg'] if nova_v else 0.0,
                        'reducaoFmt': nova_v['reducaoFmt'] if nova_v else '',
                        'historico': hist_lista,
                        'urlAudio': f'/api/audio?path={urllib.parse.quote(caminho_wav)}&t={ts_cache}',
                        'urlWav': f'/api/download?path={urllib.parse.quote(caminho_wav)}&t={ts_cache}',
                        'caminhoSrt': caminho_srt,
                        'urlSrt': f'/api/download?path={urllib.parse.quote(caminho_srt)}&t={ts_cache}' if caminho_srt else '',
                        'srtConteudo': srt_conteudo,
                        'avisoSrt': aviso_srt,
                        'similaridadeSrt': similaridade_srt
                    })

                set_progresso(f"Silêncio travado com sucesso em {len(itens_atualizados)} áudio(s)!", pct=100)
                self.responder_json({
                    'success': True,
                    'itens': itens_atualizados
                })
            except Exception as e:
                set_progresso(f"Erro ao travar silêncio em lote: {e}", pct=0)
                self.responder_json({'success': False, 'error': str(e)}, status=500)

        elif caminho == '/api/audacity/restaurar-versao':
            try:
                tamanho = int(self.headers.get('Content-Length', 0))
                corpo_bruto = self.rfile.read(tamanho)
                dados = json.loads(corpo_bruto.decode('utf-8'))

                caminho_wav = dados.get('caminhoWav', '')
                versao_id = int(dados.get('versaoId', 0))

                if not os.path.isfile(caminho_wav):
                    return self.responder_json({'success': False, 'error': f'Arquivo WAV não encontrado: {caminho_wav}'}, status=404)

                set_progresso(f"Restaurando versão {versao_id} de {os.path.basename(caminho_wav)}...")
                res = HistoricoSilencioManager.restaurar_versao(caminho_wav, versao_id)
                ts_cache = int(time.time())
                res['success'] = True
                res['urlAudio'] = f'/api/audio?path={urllib.parse.quote(caminho_wav)}&t={ts_cache}'
                res['urlWav'] = f'/api/download?path={urllib.parse.quote(caminho_wav)}&t={ts_cache}'
                res['urlSrt'] = f'/api/download?path={urllib.parse.quote(res["caminhoSrt"])}&t={ts_cache}' if res.get('caminhoSrt') else ''
                set_progresso("Versão restaurada com sucesso!")
                self.responder_json(res)
            except Exception as e:
                set_progresso(f"Erro ao restaurar versão: {e}")
                self.responder_json({'success': False, 'error': str(e)}, status=500)

        elif caminho == '/api/audacity/restaurar-lote':
            try:
                tamanho = int(self.headers.get('Content-Length', 0))
                corpo_bruto = self.rfile.read(tamanho)
                dados = json.loads(corpo_bruto.decode('utf-8'))

                itens = dados.get('itens', [])
                versao_id = int(dados.get('versaoId', 0))
                if not itens:
                    return self.responder_json({'success': False, 'error': 'Nenhum item informado.'}, status=400)

                set_progresso(f"Restaurando versão {versao_id} em {len(itens)} áudio(s)...")
                ts_cache = int(time.time())
                itens_restaurados = []
                for it in itens:
                    caminho_wav = it.get('caminhoWav') or it.get('caminho') or ''
                    if not os.path.isfile(caminho_wav):
                        continue
                    try:
                        res = HistoricoSilencioManager.restaurar_versao(caminho_wav, versao_id)
                        res['success'] = True
                        res['nome'] = it.get('nome', os.path.basename(caminho_wav))
                        res['urlAudio'] = f'/api/audio?path={urllib.parse.quote(caminho_wav)}&t={ts_cache}'
                        res['urlWav'] = f'/api/download?path={urllib.parse.quote(caminho_wav)}&t={ts_cache}'
                        res['urlSrt'] = f'/api/download?path={urllib.parse.quote(res["caminhoSrt"])}&t={ts_cache}' if res.get('caminhoSrt') else ''
                        itens_restaurados.append(res)
                    except Exception as e_res:
                        sys.stderr.write(f"[RestaurarLote] Erro em {caminho_wav}: {e_res}\n")

                set_progresso(f"{len(itens_restaurados)} áudio(s) restaurado(s) com sucesso!")
                self.responder_json({
                    'success': True,
                    'itens': itens_restaurados
                })
            except Exception as e:
                set_progresso(f"Erro ao restaurar lote: {e}")
                self.responder_json({'success': False, 'error': str(e)}, status=500)


        elif caminho == '/api/publicar':
            try:
                pasta_atual = os.path.abspath(os.path.join(BASE_DIR, '..'))
                pasta_mae = os.path.dirname(pasta_atual)
                
                # Resolução dinâmica e portátil das pastas DEV e Produção
                if any(x in pasta_atual.lower() for x in ('- dev', '-dev', 'desenvolvimento')):
                    pasta_dev = pasta_atual
                    pasta_prod = os.path.join(pasta_mae, 'Gemini TTS')
                else:
                    pasta_dev = os.path.join(pasta_mae, 'Gemini TTS - DEV')
                    pasta_prod = pasta_atual

                # Fallbacks caso as pastas não sigam o padrão exato de nomes
                if not os.path.isdir(pasta_dev) and os.path.isdir(r"C:\Projetos\Gemini TTS - DEV"):
                    pasta_dev = r"C:\Projetos\Gemini TTS - DEV"
                if not os.path.isdir(pasta_prod) and os.path.isdir(r"C:\Projetos\Gemini TTS"):
                    pasta_prod = r"C:\Projetos\Gemini TTS"

                arquivo_dev = os.path.join(pasta_dev, 'gemini-tts-studio.html')
                arquivo_prod = os.path.join(pasta_prod, 'gemini-tts-studio.html')
                arquivo_raiz = os.path.join(pasta_prod, 'Gemini TTS.html')
                pasta_backup = os.path.join(pasta_prod, 'Backups')

                if not os.path.isfile(arquivo_dev) or os.path.getsize(arquivo_dev) < 10000:
                    return self.responder_json({'success': False, 'error': 'Arquivo DEV inválido ou não encontrado.'}, status=400)

                os.makedirs(pasta_backup, exist_ok=True)
                os.makedirs(pasta_prod, exist_ok=True)

                ts = time.strftime('%Y%m%d_%H%M%S')
                backup_path = os.path.join(pasta_backup, f'gemini-tts-studio_backup_{ts}.html')

                # Backup do original antes de sobrescrever
                if os.path.isfile(arquivo_prod):
                    shutil.copy2(arquivo_prod, backup_path)
                elif os.path.isfile(arquivo_raiz):
                    shutil.copy2(arquivo_raiz, backup_path)

                # Publica no Produção e no arquivo raiz (HTML OG)
                shutil.copy2(arquivo_dev, arquivo_prod)
                shutil.copy2(arquivo_dev, arquivo_raiz)

                # Também sincroniza server e macros para a Produção
                pasta_server_dev = os.path.join(pasta_dev, 'server')
                pasta_server_prod = os.path.join(pasta_prod, 'server')
                pasta_macros_dev = os.path.join(pasta_dev, 'macros')
                pasta_macros_prod = os.path.join(pasta_prod, 'macros')

                if os.path.isdir(pasta_server_dev):
                    shutil.copytree(pasta_server_dev, pasta_server_prod, dirs_exist_ok=True)
                if os.path.isdir(pasta_macros_dev):
                    shutil.copytree(pasta_macros_dev, pasta_macros_prod, dirs_exist_ok=True)

                # Sincronização automática com o GitHub (branch main)
                github_sincronizado = False
                github_msg = ""
                repo_dir = None
                if os.path.isdir(os.path.join(pasta_mae, '_repo_gemini_tts', '.git')):
                    repo_dir = os.path.join(pasta_mae, '_repo_gemini_tts')
                elif os.path.isdir(r"C:\Projetos\_repo_gemini_tts\.git"):
                    repo_dir = r"C:\Projetos\_repo_gemini_tts"

                if repo_dir:
                    try:
                        shutil.copy2(arquivo_dev, os.path.join(repo_dir, 'gemini-tts-studio.html'))
                        if os.path.isdir(pasta_server_dev):
                            shutil.copytree(pasta_server_dev, os.path.join(repo_dir, 'server'), dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                        if os.path.isdir(pasta_macros_dev):
                            shutil.copytree(pasta_macros_dev, os.path.join(repo_dir, 'macros'), dirs_exist_ok=True)

                        subprocess.run(['git', 'checkout', 'main'], cwd=repo_dir, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
                        subprocess.run(['git', 'add', 'gemini-tts-studio.html', 'macros', 'server'], cwd=repo_dir, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
                        ts_msg = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
                        subprocess.run(['git', 'commit', '-m', f'Implantacao DEV -> Producao: {ts_msg}'], cwd=repo_dir, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
                        res_push = subprocess.run(['git', 'push', 'origin', 'main'], cwd=repo_dir, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
                        if res_push.returncode == 0:
                            github_sincronizado = True
                            github_msg = "Sincronizado e enviado com sucesso ao GitHub (branch main)!"
                        else:
                            github_msg = f"Aviso Git: {res_push.stderr or res_push.stdout}"
                    except Exception as eg:
                        github_msg = f"Aviso Git: {eg}"

                set_progresso("Versão DEV aplicada com sucesso na Produção e no GitHub!")
                self.responder_json({
                    'success': True,
                    'msg': 'Versão de Desenvolvimento aplicada com sucesso na Produção e no GitHub!',
                    'backup': backup_path,
                    'github': github_sincronizado,
                    'github_msg': github_msg
                })
            except Exception as e:
                self.responder_json({'success': False, 'error': str(e)}, status=500)

        elif caminho == '/api/github/backup':
            try:
                set_progresso("Sincronizando com o GitHub...")
                pasta_pai = os.path.abspath(os.path.join(BASE_DIR, '..'))
                pasta_mae = os.path.dirname(pasta_pai)
                
                # Detecta se está rodando no DEV ou no PROD
                is_dev = any(x in pasta_pai.lower() for x in ('- dev', '-dev', 'desenvolvimento'))

                try:
                    tamanho = int(self.headers.get('Content-Length', 0)) if self.headers.get('Content-Length') else 0
                    if tamanho > 0:
                        corpo = json.loads(self.rfile.read(tamanho).decode('utf-8'))
                        env_req = str(corpo.get('env', '')).lower()
                        if env_req in ('dev', 'desenvolvimento'):
                            is_dev = True
                        elif env_req in ('prod', 'producao'):
                            is_dev = False
                except Exception:
                    pass

                branch_destino = 'dev' if is_dev else 'main'
                pasta_origem = pasta_pai

                # Localiza repositório Git de forma dinâmica:
                # 1. Se a própria pasta já for o repositório git clonado (.git presente)
                # 2. Se houver uma pasta irmã chamada _repo_gemini_tts
                # 3. Fallback para caminho clássico C:\Projetos\_repo_gemini_tts
                repo_dir = None
                if os.path.isdir(os.path.join(pasta_origem, '.git')):
                    repo_dir = pasta_origem
                elif os.path.isdir(os.path.join(pasta_mae, '_repo_gemini_tts', '.git')):
                    repo_dir = os.path.join(pasta_mae, '_repo_gemini_tts')
                elif os.path.isdir(r"C:\Projetos\_repo_gemini_tts"):
                    repo_dir = r"C:\Projetos\_repo_gemini_tts"
                else:
                    repo_dir = pasta_origem
                
                # Se o repo for uma pasta separada, sincroniza os arquivos locais para ela antes do commit
                if os.path.abspath(repo_dir) != os.path.abspath(pasta_origem):
                    arquivo_origem = os.path.join(pasta_origem, 'gemini-tts-studio.html')
                    if os.path.isfile(arquivo_origem):
                        shutil.copy2(arquivo_origem, os.path.join(repo_dir, 'gemini-tts-studio.html'))
                    
                    pasta_server = os.path.join(pasta_origem, 'server')
                    if os.path.isdir(pasta_server):
                        shutil.copytree(pasta_server, os.path.join(repo_dir, 'server'), dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                    
                    pasta_macros = os.path.join(pasta_origem, 'macros')
                    if os.path.isdir(pasta_macros):
                        shutil.copytree(pasta_macros, os.path.join(repo_dir, 'macros'), dirs_exist_ok=True)
                
                # Garante checkout na branch correta antes de commitar e enviar
                subprocess.run(['git', 'checkout', '-B', branch_destino], cwd=repo_dir, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
                subprocess.run(['git', 'add', 'gemini-tts-studio.html', 'macros', 'server'], cwd=repo_dir, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
                subprocess.run(['git', 'commit', '--allow-empty', '-m', f'Backup ({branch_destino}): {datetime.now().strftime("%d/%m/%Y %H:%M:%S")}'], cwd=repo_dir, creationflags=subprocess.CREATE_NO_WINDOW)
                res = subprocess.run(['git', 'push', 'origin', branch_destino], cwd=repo_dir, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
                if res.returncode != 0:
                    res = subprocess.run(['git', 'push', 'origin', branch_destino, '--force'], cwd=repo_dir, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
                    if res.returncode != 0:
                        raise Exception(res.stderr or f'Erro ao enviar para a branch {branch_destino} no GitHub')
                
                set_progresso(f"Backup na branch {branch_destino} concluído com sucesso!")
                self.responder_json({
                    'success': True,
                    'branch': branch_destino,
                    'msg': f'Backup enviado e sincronizado na branch {branch_destino} do GitHub com sucesso!'
                })
            except Exception as e:
                set_progresso("Erro no backup do GitHub")
                self.responder_json({'success': False, 'error': str(e)}, status=500)

        elif caminho == '/api/processar/iniciar':
            try:
                limpar_jobs_antigos(max_idade_horas=24)
                tamanho = int(self.headers.get('Content-Length', 0))
                corpo_bruto = self.rfile.read(tamanho)
                dados = json.loads(corpo_bruto.decode('utf-8'))

                total = int(dados.get('total', 0))
                juntar = bool(dados.get('juntar', False))
                nome_macro = dados.get('macro', 'none')
                gerar_srt = bool(dados.get('gerarSrt', True))
                nome_unificado = dados.get('nome_unificado', 'audio_completo_masterizado')
                blocos_info = dados.get('blocos', [])

                job_id = f"job_{int(time.time())}_{os.getpid()}"
                raiz_dev = os.path.abspath(os.path.join(BASE_DIR, '..'))
                pasta_job = os.path.join(raiz_dev, 'Processados', job_id)
                pasta_temp = os.path.join(pasta_job, 'temp')
                pasta_saida = os.path.join(pasta_job, 'saida')
                os.makedirs(pasta_temp, exist_ok=True)
                os.makedirs(pasta_saida, exist_ok=True)

                grupos_info = dados.get('grupos', [])

                job_meta = {
                    'jobId': job_id,
                    'total': total,
                    'juntar': juntar,
                    'macro': nome_macro,
                    'gerarSrt': gerar_srt,
                    'nome_unificado': nome_unificado,
                    'texto_unificado': str(dados.get('texto_unificado', '')).strip(),
                    'pasta_job': pasta_job,
                    'pasta_temp': pasta_temp,
                    'pasta_saida': pasta_saida,
                    'blocos_info': blocos_info,
                    'grupos': grupos_info,
                    'arquivos_recebidos': {}
                }

                with JOBS_LOCK:
                    JOBS_ATIVOS[job_id] = job_meta

                meta_path = os.path.join(pasta_job, 'job_meta.json')
                with open(meta_path, 'w', encoding='utf-8') as f:
                    json.dump(job_meta, f, ensure_ascii=False, indent=2)

                sys.stderr.write(f"[Processar Stream] Sessão iniciada: {job_id} ({total} blocos esperados, {len(grupos_info)} grupos, juntar={juntar}, macro={nome_macro})\n")
                self.responder_json({'success': True, 'jobId': job_id})
            except Exception as e:
                sys.stderr.write(f"[Processar Stream] Erro em iniciar: {e}\n")
                self.responder_json({'success': False, 'error': str(e)}, status=500)

        elif caminho == '/api/processar/upload-bloco':
            try:
                query = urllib.parse.parse_qs(parsed.query)
                job_id = query.get('jobId', [''])[0]
                index = int(query.get('index', ['1'])[0])
                grupo_id = query.get('grupoId', [''])[0]
                nome_param = query.get('nome', [''])[0]

                with JOBS_LOCK:
                    job_meta = JOBS_ATIVOS.get(job_id)

                if not job_meta:
                    raiz_dev = os.path.abspath(os.path.join(BASE_DIR, '..'))
                    meta_path = os.path.join(raiz_dev, 'Processados', job_id, 'job_meta.json')
                    if os.path.isfile(meta_path):
                        with open(meta_path, 'r', encoding='utf-8') as f:
                            job_meta = json.load(f)
                        with JOBS_LOCK:
                            JOBS_ATIVOS[job_id] = job_meta

                if not job_meta:
                    return self.responder_json({'success': False, 'error': f'Job não encontrado ou expirado: {job_id}'}, status=404)

                pasta_temp = job_meta['pasta_temp']

                # Descobre nome do bloco
                nome = nome_param
                if not nome:
                    for b in job_meta.get('blocos_info', []):
                        if b.get('index') == index:
                            nome = b.get('nome', '')
                            break
                if not nome:
                    nome = f'bloco_{index:02d}'

                nome_sanitizado = "".join(c for c in nome if c not in '<>:"/\\|?*').strip() or f'bloco_{index:02d}'
                prefixo_arq = f'{grupo_id}_{index:03d}' if grupo_id else f'{index:03d}'
                if nome_sanitizado.lower().endswith(('.wav', '.mp3', '.m4a', '.ogg', '.flac')):
                    nome_sem_ext, ext_arq = os.path.splitext(nome_sanitizado)
                    caminho_arquivo = os.path.join(pasta_temp, f'{prefixo_arq}_{nome_sem_ext}{ext_arq}')
                else:
                    caminho_arquivo = os.path.join(pasta_temp, f'{prefixo_arq}_{nome_sanitizado}.wav')

                tamanho = int(self.headers.get('Content-Length', 0))
                tam_legivel = f"{tamanho // (1024*1024)} MB" if tamanho >= 1048576 else f"{tamanho // 1024} KB"
                set_progresso(f"Recebendo áudio do bloco {index} ({tam_legivel})...")

                bytes_lidos = 0
                with open(caminho_arquivo, 'wb') as f:
                    while bytes_lidos < tamanho:
                        chunk_size = min(65536, tamanho - bytes_lidos)
                        chunk = self.rfile.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        bytes_lidos += len(chunk)

                if tamanho > 0 and bytes_lidos < tamanho:
                    raise Exception(f"Upload incompleto do bloco {index}: recebidos {bytes_lidos} de {tamanho} bytes")

                chave_rec = f"{grupo_id}_{index}" if grupo_id else str(index)
                with JOBS_LOCK:
                    job_meta['arquivos_recebidos'][chave_rec] = {
                        'index': index,
                        'grupoId': grupo_id,
                        'nome': nome_sanitizado,
                        'caminho': caminho_arquivo,
                        'bytes': bytes_lidos
                    }
                    meta_path = os.path.join(job_meta['pasta_job'], 'job_meta.json')
                    try:
                        with open(meta_path, 'w', encoding='utf-8') as f:
                            json.dump(job_meta, f, ensure_ascii=False, indent=2)
                    except Exception:
                        pass

                total_esp = job_meta.get('total', '?')
                set_progresso(f"Áudio {index}/{total_esp} recebido ({tam_legivel})")
                sys.stderr.write(f"[Processar Stream] Item {chave_rec} recebido com sucesso: {caminho_arquivo} ({bytes_lidos} bytes)\n")
                self.responder_json({'success': True, 'index': index, 'bytes': bytes_lidos})
            except Exception as e:
                sys.stderr.write(f"[Processar Stream] Erro no upload do bloco: {e}\n")
                self.responder_json({'success': False, 'error': str(e)}, status=500)

        elif caminho == '/api/processar/executar':
            try:
                tamanho = int(self.headers.get('Content-Length', 0))
                corpo_bruto = self.rfile.read(tamanho)
                dados = json.loads(corpo_bruto.decode('utf-8'))
                job_id = dados.get('jobId', '')

                with JOBS_LOCK:
                    job_meta = JOBS_ATIVOS.get(job_id)

                if not job_meta:
                    raiz_dev = os.path.abspath(os.path.join(BASE_DIR, '..'))
                    meta_path = os.path.join(raiz_dev, 'Processados', job_id, 'job_meta.json')
                    if os.path.isfile(meta_path):
                        with open(meta_path, 'r', encoding='utf-8') as f:
                            job_meta = json.load(f)

                if not job_meta:
                    return self.responder_json({'success': False, 'error': f'Job não encontrado: {job_id}'}, status=404)

                arquivos_recebidos = job_meta.get('arquivos_recebidos', {})
                pasta_job = job_meta['pasta_job']
                pasta_temp = job_meta['pasta_temp']
                pasta_saida = job_meta['pasta_saida']

                # ════════════════════════════════════════════════════════
                # MODO 1: LOTE DE GRUPOS UNIFICADOS (Opção C)
                # Cada grupo tem suas partes consolidadas e todos os grupos
                # são importados simultaneamente no Audacity para masterização unificada.
                # ════════════════════════════════════════════════════════
                if job_meta.get('grupos') and len(job_meta['grupos']) > 0:
                    grupos_def = job_meta['grupos']
                    itens_grupos = []
                    roteiro_completo = []

                    for g_idx, g in enumerate(grupos_def):
                        gid = str(g.get('id', g_idx))
                        nome_g = "".join(c for c in (g.get('nome') or f'Episodio_{g_idx+1}') if c not in '<>:"/\\|?*').strip() or f'Episodio_{g_idx+1}'
                        texto_g = str(g.get('texto', '')).strip()

                        # Filtra arquivos pertencentes a este grupo
                        arqs_g = []
                        for k, v in arquivos_recebidos.items():
                            if v.get('grupoId') == gid or (len(grupos_def) == 1 and not v.get('grupoId')):
                                arqs_g.append(v)

                        # Ordena por índice da parte (1, 2, 3...)
                        arqs_g.sort(key=lambda x: int(x.get('index', 0)))

                        if not arqs_g:
                            continue

                        # Concatena todas as partes do grupo num único áudio consolidado do grupo
                        caminho_grupo_temp = os.path.join(pasta_temp, f'grupo_{g_idx:02d}_{nome_g}.wav')
                        set_progresso(f"Consolidando partes do grupo [{g_idx+1}/{len(grupos_def)}]: {nome_g}...")
                        concatenar_wavs(arqs_g, caminho_grupo_temp)

                        if texto_g:
                            roteiro_completo.append(texto_g)

                        itens_grupos.append({
                            'tipo': 'unificado',
                            'nome': nome_g,
                            'caminho_wav': caminho_grupo_temp,
                            'texto': texto_g,
                            'nome_unificado': nome_g
                        })

                    if not itens_grupos:
                        return self.responder_json({'success': False, 'error': 'Nenhum áudio de grupo foi recebido para este job.'}, status=400)

                    sys.stderr.write(f"[Processar Stream] Executando lote de {len(itens_grupos)} grupos no Audacity (Masterização Unificada - Opção C)...\n")

                    res = executar_pipeline_job(
                        itens_processar=itens_grupos,
                        roteiro_completo=roteiro_completo,
                        juntar=False, # Múltiplos grupos: processa todos juntos no Audacity e exporta cada grupo separado
                        nome_macro=job_meta.get('macro', 'none'),
                        gerar_srt=job_meta.get('gerarSrt', True),
                        pasta_job=pasta_job,
                        pasta_temp=pasta_temp,
                        pasta_saida=pasta_saida
                    )

                    with JOBS_LOCK:
                        JOBS_ATIVOS.pop(job_id, None)

                    status_code = 200 if res.get('success') else 500
                    return self.responder_json(res, status=status_code)

                # ════════════════════════════════════════════════════════
                # MODO 2: BLOCOS INDIVIDUAIS / JUNÇÃO SIMPLES
                # ════════════════════════════════════════════════════════
                blocos_info = job_meta.get('blocos_info', [])

                # Mapeamento de texto por índice
                texto_por_index = {}
                for b in blocos_info:
                    texto_por_index[int(b.get('index', 0))] = b.get('texto', '').strip()

                itens_processar = []
                roteiro_completo = []

                # Ordena arquivos recebidos pelo índice 1, 2, ...
                indices_ordenados = sorted([int(k) for k in arquivos_recebidos.keys()])
                nome_unificado = "".join(c for c in (job_meta.get('nome_unificado') or 'audio_completo_masterizado') if c not in '<>:"/\\|?*').strip() or 'audio_completo_masterizado'

                for idx in indices_ordenados:
                    item_rec = arquivos_recebidos[str(idx)]
                    txt = texto_por_index.get(idx, '')
                    if txt:
                        roteiro_completo.append(txt)
                    itens_processar.append({
                        'nome': item_rec['nome'],
                        'caminho_wav': item_rec['caminho'],
                        'texto': txt,
                        'nome_unificado': nome_unificado
                    })

                texto_unificado_custom = job_meta.get('texto_unificado', '').strip()
                if texto_unificado_custom and job_meta.get('juntar', False):
                    roteiro_completo = [texto_unificado_custom]

                if not itens_processar:
                    return self.responder_json({'success': False, 'error': 'Nenhum arquivo de áudio foi recebido para este job.'}, status=400)

                sys.stderr.write(f"[Processar Stream] Executando pipeline para job {job_id} ({len(itens_processar)} faixas)...\n")

                res = executar_pipeline_job(
                    itens_processar=itens_processar,
                    roteiro_completo=roteiro_completo,
                    juntar=job_meta.get('juntar', False),
                    nome_macro=job_meta.get('macro', 'none'),
                    gerar_srt=job_meta.get('gerarSrt', True),
                    pasta_job=job_meta['pasta_job'],
                    pasta_temp=job_meta['pasta_temp'],
                    pasta_saida=job_meta['pasta_saida']
                )

                with JOBS_LOCK:
                    JOBS_ATIVOS.pop(job_id, None)

                status_code = 200 if res.get('success') else 500
                self.responder_json(res, status=status_code)

            except Exception as e:
                sys.stderr.write(f"[Processar Stream] Erro na execução do job: {e}\n")
                self.responder_json({'success': False, 'error': str(e)}, status=500)

        elif caminho == '/api/processar':
            try:
                # Rota legada mantida para retrocompatibilidade
                limpar_jobs_antigos(max_idade_horas=24)

                tamanho = int(self.headers.get('Content-Length', 0))
                corpo_bruto = self.rfile.read(tamanho)
                dados = json.loads(corpo_bruto.decode('utf-8'))

                audios = dados.get('audios', [])
                juntar = bool(dados.get('juntar', False))
                nome_macro = dados.get('macro', 'none')
                gerar_srt = bool(dados.get('gerarSrt', True))

                if not audios:
                    return self.responder_json({'success': False, 'error': 'Nenhum áudio enviado.'}, status=400)

                raiz_dev = os.path.abspath(os.path.join(BASE_DIR, '..'))
                pasta_job = os.path.join(raiz_dev, 'Processados', f'job_{int(time.time())}')
                pasta_temp = os.path.join(pasta_job, 'temp')
                pasta_saida = os.path.join(pasta_job, 'saida')
                os.makedirs(pasta_temp, exist_ok=True)
                os.makedirs(pasta_saida, exist_ok=True)

                set_progresso("Salvando arquivos de áudio temporários...")
                itens_processar = []
                roteiro_completo = []

                for idx, a in enumerate(audios, 1):
                    nome_original = a.get('nome') or f'bloco_{idx:02d}'
                    nome_sanitizado = "".join(c for c in nome_original if c not in '<>:"/\\|?*').strip() or f'bloco_{idx:02d}'
                    texto = a.get('texto', '').strip()
                    if texto:
                        roteiro_completo.append(texto)

                    b64 = a.get('base64', '')
                    if ',' in b64:
                        b64 = b64.split(',', 1)[1]
                    caminho_wav = os.path.join(pasta_temp, f'{idx:03d}_{nome_sanitizado}.wav')
                    with open(caminho_wav, 'wb') as f:
                        f.write(base64.b64decode(b64))

                    nome_unificado_sanitizado = "".join(c for c in (audios[0].get('nome_unificado') or 'audio_completo_masterizado') if c not in '<>:"/\\|?*').strip() or 'audio_completo_masterizado'
                    itens_processar.append({
                        'nome': nome_sanitizado,
                        'caminho_wav': caminho_wav,
                        'texto': texto,
                        'nome_unificado': nome_unificado_sanitizado
                    })

                res = executar_pipeline_job(
                    itens_processar=itens_processar,
                    roteiro_completo=roteiro_completo,
                    juntar=juntar,
                    nome_macro=nome_macro,
                    gerar_srt=gerar_srt,
                    pasta_job=pasta_job,
                    pasta_temp=pasta_temp,
                    pasta_saida=pasta_saida
                )

                status_code = 200 if res.get('success') else 500
                self.responder_json(res, status=status_code)

            except Exception as e:
                set_progresso(f"Erro: {e}")
                self.responder_json({'success': False, 'error': str(e)}, status=500)

        else:
            self.responder_json({'erro': 'Rota POST não encontrada'}, status=404)

class SingleInstanceServer(ThreadingHTTPServer):
    # No Windows, SO_REUSEADDR permite que múltiplos processos escutem na mesma porta.
    # Desativar allow_reuse_address impede que dois processos bindem simultaneamente.
    allow_reuse_address = True
    request_queue_size = 128
    daemon_threads = True

def encerrar_outros_processos_bridge():
    """
    Garante que absolutamente nenhuma outra instância do bridge_server.py
    fique rodando em segundo plano. Se houver alguma, encerra imediatamente.
    """
    meu_pid = os.getpid()
    try:
        cmd = [
            'powershell', '-NoProfile', '-Command',
            f'$parent = (Get-CimInstance Win32_Process -Filter "ProcessId = {meu_pid}").ParentProcessId; '
            f'Get-CimInstance Win32_Process -Filter "Name like \'%python%\'" | '
            f'Where-Object {{ $_.ProcessId -ne {meu_pid} -and $_.ProcessId -ne $parent -and $_.CommandLine -like \'*bridge_server.py*\' }} | '
            f'ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }}'
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
        pids = [p.strip() for p in res.stdout.strip().splitlines() if p.strip()]
        if pids:
            sys.stderr.write(f"[BridgeServer] Instância(s) anterior(es) órfã(s) encerrada(s): {', '.join(pids)}\n")
    except Exception as e:
        sys.stderr.write(f"[BridgeServer] Verificação de instâncias: {e}\n")

def iniciar_servidor():
    encerrar_outros_processos_bridge()
    limpar_jobs_antigos(max_idade_horas=24)

    server = None
    for tentativa in range(8):
        try:
            server = SingleInstanceServer(('127.0.0.1', PORT), BridgeHandler)
            break
        except OSError:
            sys.stderr.write(f"[BridgeServer] Porta {PORT} ocupada ou em TimeWait. Aguardando liberação (tentativa {tentativa+1}/8)...\n")
            try:
                subprocess.run([
                    'powershell', '-NoProfile', '-Command',
                    f'$p = Get-NetTCPConnection -LocalPort {PORT} -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess; if ($p -and $p -ne {os.getpid()}) {{ Stop-Process -Id $p -Force -ErrorAction SilentlyContinue }}'
                ], timeout=4, creationflags=subprocess.CREATE_NO_WINDOW)
            except Exception:
                pass
            time.sleep(1.0)

    if not server:
        sys.stderr.write(f"[BridgeServer] Erro crítico: Não foi possível liberar a porta {PORT}.\n")
        return

    sys.stderr.write(f"[BridgeServer] Servidor multithread local ouvindo em http://127.0.0.1:{PORT}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        pipe_runner.close_audacity(force=True)

if __name__ == '__main__':
    iniciar_servidor()
