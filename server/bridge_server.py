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

# Garante que a pasta atual do server esteja no sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import pipe_runner
import transcribe_whisper

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
    """Concatena arquivos WAV preservando a taxa de amostragem."""
    caminhos = [it['caminho_wav'] for it in itens]
    if not caminhos:
        return
    with wave.open(caminhos[0], 'rb') as w_first:
        params = w_first.getparams()
    with wave.open(caminho_saida, 'wb') as w_out:
        w_out.setparams(params)
        for c in caminhos:
            with wave.open(c, 'rb') as w_in:
                w_out.writeframes(w_in.readframes(w_in.getnframes()))

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

        if gerar_srt:
            set_progresso("Whisper transcrevendo áudio unificado na GPU (última etapa)...", pct=45)
            try:
                srt_conteudo = transcribe_whisper.gerar_srt_whisper(
                    audio_final['caminho'],
                    texto_referencia=texto_ref
                )
                caminho_srt = os.path.join(pasta_saida, f"{audio_final['nome']}.srt")
                with open(caminho_srt, 'w', encoding='utf-8') as sf:
                    sf.write(srt_conteudo)
                set_progresso("Legenda unificada gerada com sucesso!", pct=95)
            except Exception as ew:
                sys.stderr.write(f"[Whisper] Erro na transcrição: {ew}\n")

        meta = calcular_metadados_audio(audio_final['caminho'])

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
            'srtConteudo': srt_conteudo
        })

    else:
        # Bloco a bloco: Whisper gera o SRT de cada um após a exportação
        total_bl = len(arquivos_audacity)
        for idx, arq in enumerate(arquivos_audacity):
            texto_ref = itens_processar[idx]['texto'] if idx < len(itens_processar) else ""
            srt_conteudo = ""
            caminho_srt = ""

            if gerar_srt:
                pct_inicio = 20 + int((idx / total_bl) * 75)
                set_progresso(f"Whisper transcrevendo bloco {idx+1}/{total_bl} na GPU...", pct=pct_inicio)
                try:
                    srt_conteudo = transcribe_whisper.gerar_srt_whisper(
                        arq['caminho'],
                        texto_referencia=texto_ref
                    )
                    caminho_srt = os.path.join(pasta_saida, f"{arq['nome']}.srt")
                    with open(caminho_srt, 'w', encoding='utf-8') as sf:
                        sf.write(srt_conteudo)
                    pct_fim = 20 + int(((idx + 1) / total_bl) * 75)
                    set_progresso(f"Bloco {idx+1}/{total_bl} concluído!", pct=pct_fim)
                except Exception as ew:
                    sys.stderr.write(f"[Whisper] Erro no bloco {idx+1}: {ew}\n")

            meta = calcular_metadados_audio(arq['caminho'])

            itens_finais.append({
                'tipo': 'bloco',
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
                'srtConteudo': srt_conteudo
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
        self.end_headers()
        self.wfile.write(corpo)

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
                srt_conteudo = transcribe_whisper.gerar_srt_whisper(
                    caminho_wav,
                    texto_referencia=texto_ref
                )
                caminho_srt = os.path.splitext(caminho_wav)[0] + '.srt'
                with open(caminho_srt, 'w', encoding='utf-8') as sf:
                    sf.write(srt_conteudo)

                set_progresso("Legenda SRT gerada com sucesso!")
                self.responder_json({
                    'success': True,
                    'caminhoSrt': caminho_srt,
                    'urlSrt': f'/api/download?path={urllib.parse.quote(caminho_srt)}',
                    'srtConteudo': srt_conteudo
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

                set_progresso(f"Aplicando corte de silêncio ({duracao}s / {compressao}%) no Audacity: {os.path.basename(caminho_wav)}...")
                res_audacity = pipe_runner.executar_travar_silencio(
                    caminho_wav,
                    duracao=duracao,
                    compressao=compressao,
                    limiar=limiar,
                    descartar=descartar,
                    progress_callback=set_progresso
                )

                if not res_audacity.get('success'):
                    set_progresso("Falha ao travar silêncio no Audacity.")
                    return self.responder_json(res_audacity, status=500)

                # Recalcula metadados de áudio após redução de silêncio
                meta = calcular_metadados_audio(caminho_wav)

                # Se o arquivo já possuía legenda .srt ou foi solicitado gerar,
                # regera o SRT via Whisper para que as legendas fiquem 100% sincronizadas com os novos tempos
                caminho_srt_existente = os.path.splitext(caminho_wav)[0] + '.srt'
                deve_regerar_srt = gerar_srt or os.path.isfile(caminho_srt_existente)
                srt_conteudo = ""
                caminho_srt = ""

                if deve_regerar_srt:
                    set_progresso("Ressincronizando legenda SRT no Whisper para os novos tempos do áudio...")
                    try:
                        srt_conteudo = transcribe_whisper.gerar_srt_whisper(
                            caminho_wav,
                            texto_referencia=texto_ref
                        )
                        caminho_srt = caminho_srt_existente
                        with open(caminho_srt, 'w', encoding='utf-8') as sf:
                            sf.write(srt_conteudo)
                    except Exception as ew:
                        sys.stderr.write(f"[Whisper] Erro ao ressincronizar SRT pós-corte: {ew}\n")

                set_progresso("Silêncio travado e áudio atualizado com sucesso!")
                ts_cache = int(time.time())
                self.responder_json({
                    'success': True,
                    'caminhoWav': caminho_wav,
                    'tamanhoFmt': meta['tamanho_fmt'],
                    'tamanhoBytes': meta['tamanho_bytes'],
                    'duracaoFmt': meta['duracao_fmt'],
                    'duracaoSeg': meta['duracao_seg'],
                    'urlAudio': f'/api/audio?path={urllib.parse.quote(caminho_wav)}&t={ts_cache}',
                    'urlWav': f'/api/download?path={urllib.parse.quote(caminho_wav)}&t={ts_cache}',
                    'caminhoSrt': caminho_srt,
                    'urlSrt': f'/api/download?path={urllib.parse.quote(caminho_srt)}&t={ts_cache}' if caminho_srt else '',
                    'srtConteudo': srt_conteudo
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

                set_progresso(f"Iniciando corte de silêncio em lote no Audacity ({len(itens)} áudio(s))...")
                res_lote = pipe_runner.executar_travar_silencio_lote(
                    itens,
                    duracao=duracao,
                    compressao=compressao,
                    limiar=limiar,
                    descartar=descartar,
                    progress_callback=set_progresso
                )

                if not res_lote.get('success'):
                    set_progresso("Falha no corte de silêncio em lote no Audacity.")
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
                    deve_regerar_srt = it.get('gerarSrt', True) or os.path.isfile(caminho_srt_existente)
                    srt_conteudo = ""
                    caminho_srt = ""

                    if deve_regerar_srt:
                        set_progresso(f"Ressincronizando SRT no Whisper ({idx}/{total}): {os.path.basename(caminho_wav)}...")
                        try:
                            srt_conteudo = transcribe_whisper.gerar_srt_whisper(
                                caminho_wav,
                                texto_referencia=it.get('textoReferencia', '')
                            )
                            caminho_srt = caminho_srt_existente
                            with open(caminho_srt, 'w', encoding='utf-8') as sf:
                                sf.write(srt_conteudo)
                        except Exception as ew:
                            sys.stderr.write(f"[Whisper] Erro ao ressincronizar SRT em lote: {ew}\n")

                    ts_cache = int(time.time())
                    itens_atualizados.append({
                        'success': True,
                        'caminhoWav': caminho_wav,
                        'nome': it.get('nome', os.path.basename(caminho_wav)),
                        'tamanhoFmt': meta['tamanho_fmt'],
                        'tamanhoBytes': meta['tamanho_bytes'],
                        'duracaoFmt': meta['duracao_fmt'],
                        'duracaoSeg': meta['duracao_seg'],
                        'urlAudio': f'/api/audio?path={urllib.parse.quote(caminho_wav)}&t={ts_cache}',
                        'urlWav': f'/api/download?path={urllib.parse.quote(caminho_wav)}&t={ts_cache}',
                        'caminhoSrt': caminho_srt,
                        'urlSrt': f'/api/download?path={urllib.parse.quote(caminho_srt)}&t={ts_cache}' if caminho_srt else '',
                        'srtConteudo': srt_conteudo
                    })

                set_progresso(f"Silêncio travado com sucesso em {len(itens_atualizados)} áudio(s)!")
                self.responder_json({
                    'success': True,
                    'itens': itens_atualizados
                })
            except Exception as e:
                set_progresso(f"Erro ao travar silêncio em lote: {e}")
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

                        subprocess.run(['git', 'checkout', 'main'], cwd=repo_dir, capture_output=True, text=True)
                        subprocess.run(['git', 'add', 'gemini-tts-studio.html', 'macros', 'server'], cwd=repo_dir, check=True)
                        ts_msg = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
                        subprocess.run(['git', 'commit', '-m', f'Implantacao DEV -> Producao: {ts_msg}'], cwd=repo_dir, capture_output=True, text=True)
                        res_push = subprocess.run(['git', 'push', 'origin', 'main'], cwd=repo_dir, capture_output=True, text=True)
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
                subprocess.run(['git', 'checkout', '-B', branch_destino], cwd=repo_dir, check=True)
                subprocess.run(['git', 'add', 'gemini-tts-studio.html', 'macros', 'server'], cwd=repo_dir, check=True)
                subprocess.run(['git', 'commit', '--allow-empty', '-m', f'Backup ({branch_destino}): {datetime.now().strftime("%d/%m/%Y %H:%M:%S")}'], cwd=repo_dir)
                res = subprocess.run(['git', 'push', 'origin', branch_destino], cwd=repo_dir, capture_output=True, text=True)
                if res.returncode != 0:
                    res = subprocess.run(['git', 'push', 'origin', branch_destino, '--force'], cwd=repo_dir, capture_output=True, text=True)
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
                    'arquivos_recebidos': {}
                }

                with JOBS_LOCK:
                    JOBS_ATIVOS[job_id] = job_meta

                meta_path = os.path.join(pasta_job, 'job_meta.json')
                with open(meta_path, 'w', encoding='utf-8') as f:
                    json.dump(job_meta, f, ensure_ascii=False, indent=2)

                sys.stderr.write(f"[Processar Stream] Sessão iniciada: {job_id} ({total} blocos esperados, juntar={juntar}, macro={nome_macro})\n")
                self.responder_json({'success': True, 'jobId': job_id})
            except Exception as e:
                sys.stderr.write(f"[Processar Stream] Erro em iniciar: {e}\n")
                self.responder_json({'success': False, 'error': str(e)}, status=500)

        elif caminho == '/api/processar/upload-bloco':
            try:
                query = urllib.parse.parse_qs(parsed.query)
                job_id = query.get('jobId', [''])[0]
                index = int(query.get('index', ['1'])[0])
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
                if nome_sanitizado.lower().endswith(('.wav', '.mp3', '.m4a', '.ogg', '.flac')):
                    nome_sem_ext, ext_arq = os.path.splitext(nome_sanitizado)
                    caminho_arquivo = os.path.join(pasta_temp, f'{index:03d}_{nome_sem_ext}{ext_arq}')
                else:
                    caminho_arquivo = os.path.join(pasta_temp, f'{index:03d}_{nome_sanitizado}.wav')

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

                with JOBS_LOCK:
                    job_meta['arquivos_recebidos'][str(index)] = {
                        'index': index,
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

                sys.stderr.write(f"[Processar Stream] Bloco {index} recebido com sucesso: {caminho_arquivo} ({bytes_lidos} bytes)\n")
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
    allow_reuse_address = False

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
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
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
                ], timeout=4)
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
