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

def set_progresso(msg):
    global PROGRESSO_ATUAL
    PROGRESSO_ATUAL = msg
    sys.stderr.write(f"[Status] {msg}\n")

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

class BridgeHandler(BaseHTTPRequestHandler):
    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

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
                'progresso': PROGRESSO_ATUAL
            })

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

        elif caminho == '/api/audacity/travar-silencio':
            try:
                tamanho = int(self.headers.get('Content-Length', 0))
                corpo_bruto = self.rfile.read(tamanho)
                dados = json.loads(corpo_bruto.decode('utf-8'))

                caminho_wav = dados.get('caminhoWav', '')
                texto_ref = dados.get('textoReferencia', '')
                gerar_srt = bool(dados.get('gerarSrt', True))

                # Suporte a presets e configurações dinâmicas
                preset = dados.get('preset', '2.0_40')
                if preset in ('2s_40', '2.0_40', '2_40'):
                    duracao, compressao = '2', '40'
                elif preset in ('0.5s_60', '0.5_60'):
                    duracao, compressao = '0,5', '60'
                elif preset in ('1.3s_30', '1.3_30'):
                    duracao, compressao = '1,3', '30'
                else:
                    duracao = str(dados.get('duracao', '2'))
                    compressao = str(dados.get('compressao', '40'))

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

        elif caminho == '/api/publicar':
            try:
                raiz = os.path.abspath(os.path.join(BASE_DIR, '..', '..'))
                arquivo_dev = os.path.join(raiz, 'Desenvolvimento', 'gemini-tts-studio.html')
                pasta_prod = os.path.join(raiz, 'Producao')
                arquivo_prod = os.path.join(pasta_prod, 'gemini-tts-studio.html')
                arquivo_raiz = os.path.join(raiz, 'Gemini TTS.html')
                pasta_backup = os.path.join(raiz, 'Backups')

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
                pasta_server_dev = os.path.join(raiz, 'Desenvolvimento', 'server')
                pasta_server_prod = os.path.join(raiz, 'Producao', 'server')
                pasta_macros_dev = os.path.join(raiz, 'Desenvolvimento', 'macros')
                pasta_macros_prod = os.path.join(raiz, 'Producao', 'macros')

                if os.path.isdir(pasta_server_dev):
                    shutil.copytree(pasta_server_dev, pasta_server_prod, dirs_exist_ok=True)
                if os.path.isdir(pasta_macros_dev):
                    shutil.copytree(pasta_macros_dev, pasta_macros_prod, dirs_exist_ok=True)

                set_progresso("Versão DEV aplicada com sucesso no HTML Original (PROD)!")
                self.responder_json({
                    'success': True,
                    'msg': 'Versão de Desenvolvimento aplicada com sucesso no HTML original (PROD)!',
                    'backup': backup_path
                })
            except Exception as e:
                self.responder_json({'success': False, 'error': str(e)}, status=500)

        elif caminho == '/api/github/backup':
            try:
                set_progresso("Sincronizando com o GitHub...")
                repo_dir = r"C:\Projetos\_repo_gemini_tts"
                
                # Sincroniza os arquivos locais para a pasta do repositório git
                arquivo_origem = os.path.join(os.path.dirname(__file__), '..', 'gemini-tts-studio.html')
                if os.path.isfile(arquivo_origem):
                    shutil.copy2(arquivo_origem, os.path.join(repo_dir, 'gemini-tts-studio.html'))
                
                pasta_server = os.path.dirname(__file__)
                shutil.copytree(pasta_server, os.path.join(repo_dir, 'server'), dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
                
                pasta_macros = os.path.join(os.path.dirname(__file__), '..', 'macros')
                if os.path.isdir(pasta_macros):
                    shutil.copytree(pasta_macros, os.path.join(repo_dir, 'macros'), dirs_exist_ok=True)
                
                # Executa o git commit e git push usando as credenciais do sistema
                subprocess.run(['git', 'add', 'gemini-tts-studio.html', 'macros', 'server'], cwd=repo_dir, check=True)
                subprocess.run(['git', 'commit', '-m', f'Backup do app: {datetime.now().strftime("%d/%m/%Y %H:%M:%S")}'], cwd=repo_dir)
                res = subprocess.run(['git', 'push', 'origin', 'main'], cwd=repo_dir, capture_output=True, text=True)
                if res.returncode != 0:
                    raise Exception(res.stderr or 'Erro ao enviar para o GitHub')
                
                set_progresso("Backup no GitHub concluído com sucesso!")
                self.responder_json({
                    'success': True,
                    'msg': 'Backup enviado e sincronizado no GitHub com sucesso!'
                })
            except Exception as e:
                set_progresso("Erro no backup do GitHub")
                self.responder_json({'success': False, 'error': str(e)}, status=500)

        elif caminho == '/api/processar':
            try:
                # Remove automaticamente arquivos temporários de jobs com mais de 24 horas
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

                sys.stderr.write(f"[Processar] Recebidos {len(audios)} áudio(s) para processar. Juntar={juntar}, Macro={nome_macro}, GerarSrt={gerar_srt}\n")
                for idx_log, a_log in enumerate(audios, 1):
                    sys.stderr.write(f"  -> Bloco {idx_log}: nome='{a_log.get('nome')}', texto_chars={len(a_log.get('texto', ''))}\n")

                # Cria pasta temporária para o job atual
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
                    nome = a.get('nome') or f'bloco_{idx:02d}'
                    texto = a.get('texto', '').strip()
                    if texto:
                        roteiro_completo.append(texto)

                    b64 = a.get('base64', '')
                    if ',' in b64:
                        b64 = b64.split(',', 1)[1]
                    caminho_wav = os.path.join(pasta_temp, f'{idx:03d}_{nome}.wav')
                    with open(caminho_wav, 'wb') as f:
                        f.write(base64.b64decode(b64))

                    itens_processar.append({
                        'nome': nome,
                        'caminho_wav': caminho_wav,
                        'texto': texto,
                        'nome_unificado': audios[0].get('nome_unificado') or 'audio_completo_masterizado'
                    })

                # Acha caminho da macro
                macro_path = None
                if nome_macro and nome_macro != 'none':
                    for m in pipe_runner.list_available_macros():
                        if m['arquivo'] == nome_macro or m['nome'] == nome_macro:
                            macro_path = m['caminho']
                            break

                # 1. Executa Audacity
                res_audacity = pipe_runner.executar_processamento_audacity(
                    itens_processar,
                    juntar=juntar,
                    macro_path=macro_path,
                    pasta_saida=pasta_saida,
                    progress_callback=set_progresso
                )

                if not res_audacity.get('success'):
                    set_progresso("Falha no Audacity.")
                    return self.responder_json(res_audacity, status=500)

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
                        set_progresso("Whisper transcrevendo áudio unificado na GPU (última etapa)...")
                        try:
                            srt_conteudo = transcribe_whisper.gerar_srt_whisper(
                                audio_final['caminho'],
                                texto_referencia=texto_ref
                            )
                            caminho_srt = os.path.join(pasta_saida, f"{audio_final['nome']}.srt")
                            with open(caminho_srt, 'w', encoding='utf-8') as sf:
                                sf.write(srt_conteudo)
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
                    for idx, arq in enumerate(arquivos_audacity):
                        texto_ref = itens_processar[idx]['texto'] if idx < len(itens_processar) else ""
                        srt_conteudo = ""
                        caminho_srt = ""

                        if gerar_srt:
                            set_progresso(f"Whisper gerando legenda do bloco {idx+1}/{len(arquivos_audacity)} (última etapa)...")
                            try:
                                srt_conteudo = transcribe_whisper.gerar_srt_whisper(
                                    arq['caminho'],
                                    texto_referencia=texto_ref
                                )
                                caminho_srt = os.path.join(pasta_saida, f"{arq['nome']}.srt")
                                with open(caminho_srt, 'w', encoding='utf-8') as sf:
                                    sf.write(srt_conteudo)
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

                set_progresso("Processamento concluído com sucesso!")
                self.responder_json({
                    'success': True,
                    'juntar': juntar,
                    'pastaJob': pasta_job,
                    'itens': itens_finais
                })

            except Exception as e:
                set_progresso(f"Erro: {e}")
                self.responder_json({'success': False, 'error': str(e)}, status=500)
            finally:
                # Mantém o Audacity em prontidão para novos lotes (não fecha entre gerações)
                pass

        else:
            self.responder_json({'erro': 'Rota POST não encontrada'}, status=404)

def iniciar_servidor():
    # Limpeza preventiva de arquivos temporários com mais de 24h ao iniciar
    limpar_jobs_antigos(max_idade_horas=24)

    server = ThreadingHTTPServer(('127.0.0.1', PORT), BridgeHandler)
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
