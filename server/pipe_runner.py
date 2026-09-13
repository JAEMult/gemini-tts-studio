# -*- coding: utf-8 -*-
import os
import sys
import time
import json
import glob
import ctypes
from ctypes import wintypes
import subprocess
import shutil
import threading

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3

kernel32 = ctypes.windll.kernel32
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
]
INVALID_HANDLE = wintypes.HANDLE(-1).value

PIPE_TO = r'\\.\pipe\ToSrvPipe'
PIPE_FROM = r'\\.\pipe\FromSrvPipe'

AUDACITY_INICIADO_POR_NOS = False
AUDACITY_LOCK = threading.Lock()

# Configuração padrão de corte/compressão de silêncio excessivo (1,3s / 30%)
CMD_TRAVAR_SILENCIO_FINAL = 'TruncateSilence:Action="Compress Excess Silence" Compress="30" Independent="1" Minimum="1,3" Threshold="-35" Truncate="0,5" TruncateEnd="1" TruncateMiddle="1" TruncateStart="1"'

def limpar_todas_faixas(client):
    """Garante que absolutamente nenhuma faixa residual permaneça no Audacity."""
    for _ in range(5):
        try:
            info = client.send('GetInfo: Type=Tracks', timeout=2.5)
            # Se não há faixas abertas, GetInfo retorna '[  ]' ou sem '"name"'
            if '"name"' not in info:
                return True
            client.send('SelectAll:', timeout=2.0)
            client.send('RemoveTracks:', timeout=2.0)
            time.sleep(0.15)
        except Exception:
            pass
    return False

def find_audacity_exe():
    """
    Localiza dinamicamente o executável do Audacity no computador:
    1. Se o processo já estiver rodando, descobre o caminho pelo sistema.
    2. Procura nas chaves de Registro do Windows (HKLM, WOW6432Node, HKCU e App Paths).
    3. Procura no PATH do sistema.
    4. Procura nas pastas de instalação padrão (%ProgramFiles%, %ProgramFiles(x86)%, %LOCALAPPDATA%, C:, D:, etc.).
    Se não encontrar, retorna None (NUNCA baixa nada automaticamente; o usuário deve ter o Audacity).
    """
    # 1. Se já está rodando, descobre caminho do executável ativo
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-Command', '(Get-Process -Name Audacity -ErrorAction SilentlyContinue).Path'],
            capture_output=True, text=True, timeout=3
        )
        for line in out.stdout.strip().splitlines():
            p = line.strip()
            if p and os.path.isfile(p):
                return os.path.abspath(p)
    except Exception:
        pass

    # 2. PATH do Windows
    w = shutil.which('audacity') or shutil.which('Audacity')
    if w and os.path.isfile(w):
        return os.path.abspath(w)

    # 3. Registro do Windows
    try:
        import winreg
        chaves_busca = [
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Audacity.exe'),
            (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Audacity.exe'),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
            (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'),
            (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
        ]
        for hkey, subkey in chaves_busca:
            try:
                with winreg.OpenKey(hkey, subkey) as k:
                    if 'App Paths' in subkey:
                        try:
                            val, _ = winreg.QueryValueEx(k, '')
                            val = str(val).strip('\"\'')
                            if val and os.path.isfile(val):
                                return os.path.abspath(val)
                        except Exception:
                            pass
                    else:
                        num_subkeys = winreg.QueryInfoKey(k)[0]
                        for i in range(num_subkeys):
                            try:
                                sub = winreg.EnumKey(k, i)
                                with winreg.OpenKey(k, sub) as sk:
                                    try:
                                        display_name, _ = winreg.QueryValueEx(sk, 'DisplayName')
                                    except Exception:
                                        display_name = ''
                                    if 'audacity' in str(display_name).lower():
                                        for prop in ['InstallLocation', 'DisplayIcon']:
                                            try:
                                                val, _ = winreg.QueryValueEx(sk, prop)
                                                caminho = str(val).strip('\"\'')
                                                if caminho.lower().endswith('.exe') and os.path.isfile(caminho):
                                                    return os.path.abspath(caminho)
                                                cand = os.path.join(caminho, 'Audacity.exe')
                                                if os.path.isfile(cand):
                                                    return os.path.abspath(cand)
                                            except Exception:
                                                pass
                            except Exception:
                                pass
            except Exception:
                pass
    except Exception:
        pass

    # 4. Pastas padrão no Windows
    locais_comuns = [
        os.path.expandvars(r'%ProgramFiles%\Audacity\Audacity.exe'),
        os.path.expandvars(r'%ProgramFiles(x86)%\Audacity\Audacity.exe'),
        os.path.expandvars(r'%LOCALAPPDATA%\Programs\Audacity\Audacity.exe'),
        r'C:\Program Files\Audacity\Audacity.exe',
        r'C:\Program Files (x86)\Audacity\Audacity.exe',
    ]
    for disco in ['C', 'D', 'E', 'F', 'G']:
        locais_comuns.append(f'{disco}:\\Audacity\\Audacity.exe')
        locais_comuns.append(f'{disco}:\\Program Files\\Audacity\\Audacity.exe')

    for p in locais_comuns:
        if os.path.isfile(p):
            return os.path.abspath(p)

    return None

def get_macros_dirs():
    """Retorna exclusivamente a pasta oficial de macros do próprio projeto."""
    pastas = []
    local_proj = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'macros'))
    if os.path.isdir(local_proj):
        pastas.append(local_proj)
    return pastas

def list_available_macros():
    """Retorna a lista de macros encontradas, sem duplicatas, ignorando macros de junção estrutural."""
    macros = []
    nomes_vistos = set()
    for d in get_macros_dirs():
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.lower().endswith('.txt'):
                nome = os.path.splitext(f)[0]
                # Ignora arquivos de macro que apenas juntam áudios, pois a junção é controlada pela checkbox
                if 'juntar' in nome.lower():
                    continue
                if nome not in nomes_vistos:
                    nomes_vistos.add(nome)
                    macros.append({
                        'nome': nome,
                        'arquivo': f,
                        'caminho': os.path.join(d, f)
                    })
    return macros

def clean_audacity_sessions():
    """
    Remove arquivos temporários residuais de sessões do Audacity (.aup3unsaved*)
    que causam a exibição da janela modal 'Recuperação Automática de Falhas'.
    """
    try:
        session_dir = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'audacity', 'SessionData')
        if os.path.exists(session_dir):
            for f in glob.glob(os.path.join(session_dir, '*.aup3unsaved*')):
                try:
                    os.remove(f)
                except Exception:
                    pass
    except Exception:
        pass

def ensure_audacity_cfg():
    """Garante que o módulo de script pipe esteja ativo e todas as telas de splash/intro/ajuda desativadas."""
    appdata = os.environ.get('APPDATA', '')
    cfg_path = os.path.join(appdata, 'audacity', 'audacity.cfg')
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            changed = False
            if 'mod-script-pipe=1' not in content:
                content = content.replace('mod-script-pipe=4', 'mod-script-pipe=1')
                if 'mod-script-pipe=1' not in content:
                    content += '\n[Module]\nmod-script-pipe=1\n'
                changed = True

            # Desativa tela de splash inicial e diálogos de boas-vindas
            if 'ShowSplashScreen=1' in content:
                content = content.replace('ShowSplashScreen=1', 'ShowSplashScreen=0')
                changed = True
            elif 'ShowSplashScreen=0' not in content:
                content = content.replace('[GUI]', '[GUI]\nShowSplashScreen=0')
                changed = True

            if 'IntroOrderStart=1' in content:
                content = content.replace('IntroOrderStart=1', 'IntroOrderStart=0')
                changed = True

            if 'ShowHowToGetHelpAtLaunch=1' in content:
                content = content.replace('ShowHowToGetHelpAtLaunch=1', 'ShowHowToGetHelpAtLaunch=0')
                changed = True
            elif 'ShowHowToGetHelpAtLaunch=0' not in content:
                content = content.replace('[GUI]', '[GUI]\nShowHowToGetHelpAtLaunch=0\nShowHelpAtLaunch=0')
                changed = True

            if '[Update]' in content:
                if 'DefaultUpdatesChecking=1' in content:
                    content = content.replace('DefaultUpdatesChecking=1', 'DefaultUpdatesChecking=0')
                    changed = True
                if 'UpdateNoticeShown=0' in content:
                    content = content.replace('UpdateNoticeShown=0', 'UpdateNoticeShown=1')
                    changed = True

            if changed:
                with open(cfg_path, 'w', encoding='utf-8') as f:
                    f.write(content)
        except Exception as e:
            sys.stderr.write(f'[AudacityCfg] Erro ao verificar cfg: {e}\n')

def is_audacity_running():
    out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq Audacity.exe'], capture_output=True, text=True)
    return 'Audacity.exe' in out.stdout

# ════════════════════════════════════════════════════════════════
# SILENCIADOR DE JANELAS DO AUDACITY
# Impede que o splash ("O Audacity está iniciando..."), diálogos ou
# a própria janela principal saltem na tela do usuário.
# ════════════════════════════════════════════════════════════════
_SILENCER_ACTIVE = False
_SILENCER_THREAD = None
_AUDACITY_PID_CACHE = {}

def _is_audacity_pid(pid):
    if not pid:
        return False
    cached = _AUDACITY_PID_CACHE.get(pid)
    if cached is not None:
        return cached
    h = kernel32.OpenProcess(0x1000, False, pid) # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            res = 'audacity.exe' in os.path.basename(buf.value).lower()
            _AUDACITY_PID_CACHE[pid] = res
            return res
    finally:
        kernel32.CloseHandle(h)
    return False

def _silencer_worker():
    global _SILENCER_ACTIVE
    user32 = ctypes.windll.user32
    # SWP_NOACTIVATE (0x0010) | SWP_NOSIZE (0x0001) | SWP_NOZORDER (0x0004)
    SWP_FLAGS = 0x0010 | 0x0001 | 0x0004
    while _SILENCER_ACTIVE:
        try:
            def enum_cb(hwnd, lparam):
                try:
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if pid.value and _is_audacity_pid(pid.value):
                        length = user32.GetWindowTextLengthW(hwnd)
                        title = ''
                        if length > 0:
                            buf = ctypes.create_unicode_buffer(length + 1)
                            user32.GetWindowTextW(hwnd, buf, length + 1)
                            title = buf.value

                        t_lower = title.lower()
                        # Diálogos de splash, inicialização ("O Audacity está iniciando..."), recuperação ou avisos:
                        # Move fisicamente para fora do monitor e oculta com SW_HIDE (0)
                        if not title or any(w in t_lower for w in ['iniciando', 'starting', 'recupera', 'recovery', 'splash', 'welcome', 'ajuda', 'help', 'aviso', 'notice']):
                            user32.SetWindowPos(hwnd, 0, -32000, -32000, 0, 0, SWP_FLAGS | 0x0080) # SWP_HIDEWINDOW
                            user32.ShowWindow(hwnd, 0) # SW_HIDE
                        else:
                            # Janela principal do Audacity: move para fora do monitor e minimiza na barra
                            if user32.IsWindowVisible(hwnd):
                                user32.SetWindowPos(hwnd, 0, -32000, -32000, 0, 0, SWP_FLAGS)
                                user32.ShowWindow(hwnd, 6) # SW_MINIMIZE
                except Exception:
                    pass
                return True

            EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows(EnumProc(enum_cb), 0)
        except Exception:
            pass
        time.sleep(0.04)

def start_audacity_silencer():
    """Inicia thread sentinela que oculta instantaneamente qualquer tela do Audacity."""
    global _SILENCER_ACTIVE, _SILENCER_THREAD
    if not _SILENCER_ACTIVE:
        _SILENCER_ACTIVE = True
        _SILENCER_THREAD = threading.Thread(target=_silencer_worker, daemon=True)
        _SILENCER_THREAD.start()

def stop_audacity_silencer():
    """Interrompe a thread sentinela."""
    global _SILENCER_ACTIVE
    _SILENCER_ACTIVE = False

def minimize_audacity():
    """Garante que a janela do Audacity fique minimizada e não salte na tela."""
    start_audacity_silencer()

def launch_audacity():
    """Inicia o Audacity 100% invisível/minimizado em segundo plano, sem nenhuma intro ou janela na tela."""
    global AUDACITY_INICIADO_POR_NOS
    if is_audacity_running():
        sys.stderr.write('[Audacity] Audacity já estava em execução no sistema. Mantendo instância existente silenciosa.\n')
        start_audacity_silencer()
        return

    audacity_exe = find_audacity_exe()
    if not audacity_exe:
        raise FileNotFoundError(
            'Audacity não foi encontrado no seu computador!\n'
            'Por favor, instale o Audacity pelo site oficial (https://www.audacityteam.org/download/) '
            'para que o estúdio possa masterizar seus áudios.'
        )

    AUDACITY_INICIADO_POR_NOS = True
    clean_audacity_sessions()
    ensure_audacity_cfg()
    start_audacity_silencer()

    subprocess.Popen(['cmd.exe', '/c', 'start', '/min', '', audacity_exe])

def close_audacity(force=False):
    """
    Encerra o Audacity se ele foi iniciado pelo estúdio ou se force=True.
    Se já estava aberto antes pelo usuário, NÃO fecha para não interferir em seus trabalhos.
    """
    global AUDACITY_INICIADO_POR_NOS, _AUDACITY_PID_CACHE
    stop_audacity_silencer()
    _AUDACITY_PID_CACHE.clear()

    if not is_audacity_running():
        AUDACITY_INICIADO_POR_NOS = False
        clean_audacity_sessions()
        return

    if not force and not AUDACITY_INICIADO_POR_NOS:
        sys.stderr.write('[Audacity] Preservando Audacity aberto (foi iniciado previamente pelo usuário).\n')
        return

    sys.stderr.write('[Audacity] Encerrando o Audacity iniciado pelo estúdio...\n')
    subprocess.run(['taskkill', '/IM', 'Audacity.exe'], capture_output=True, text=True)
    time.sleep(1.0)
    if is_audacity_running():
        subprocess.run(['taskkill', '/F', '/IM', 'Audacity.exe'], capture_output=True, text=True)
        time.sleep(0.5)
    AUDACITY_INICIADO_POR_NOS = False
    clean_audacity_sessions()
    sys.stderr.write('[Audacity] Processo do Audacity finalizado.\n')

class PipeClient:
    def __init__(self):
        self.h_to = None
        self.h_from = None

    def connect(self, timeout=12.0):
        start = time.time()
        while time.time() - start < timeout:
            h_to = kernel32.CreateFileW(PIPE_TO, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
            if h_to != INVALID_HANDLE:
                h_from = kernel32.CreateFileW(PIPE_FROM, GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
                if h_from != INVALID_HANDLE:
                    self.h_to = h_to
                    self.h_from = h_from
                    return True
                kernel32.CloseHandle(h_to)
            time.sleep(0.4)
        return False

    def send(self, cmd, timeout=30.0):
        if not self.h_to or not self.h_from:
            raise Exception('Pipes do Audacity não estão conectados.')

        cmd_bytes = (cmd.strip() + '\n').encode('utf-8')
        written = wintypes.DWORD()
        kernel32.WriteFile(self.h_to, cmd_bytes, len(cmd_bytes), ctypes.byref(written), None)

        start = time.time()
        res = []
        bytes_avail = wintypes.DWORD()

        while time.time() - start < timeout:
            ok = kernel32.PeekNamedPipe(self.h_from, None, 0, None, ctypes.byref(bytes_avail), None)
            if ok and bytes_avail.value > 0:
                buf = ctypes.create_string_buffer(bytes_avail.value)
                read_bytes = wintypes.DWORD()
                if kernel32.ReadFile(self.h_from, buf, bytes_avail.value, ctypes.byref(read_bytes), None):
                    chunk = buf.raw[:read_bytes.value].decode('utf-8', errors='ignore')
                    res.append(chunk)
                    if 'BatchCommand finished:' in chunk or (len(chunk) >= 2 and chunk.endswith('\n\n')):
                        break
            time.sleep(0.04)

        return ''.join(res).strip()

    def close(self):
        if self.h_to:
            try: kernel32.CloseHandle(self.h_to)
            except: pass
            self.h_to = None
        if self.h_from:
            try: kernel32.CloseHandle(self.h_from)
            except: pass
            self.h_from = None

def executar_processamento_audacity(itens_audio, juntar=True, macro_path=None, pasta_saida='', progress_callback=None):
    """
    Executa a automação no Audacity:
    1. Protegido com AUDACITY_LOCK (evita concorrência e sobreposição entre requisições).
    2. Se juntar=True:
       - Limpa projeto do Audacity (garante 0 faixas residuais)
       - Importa todos os blocos na ordem
       - Se houver mais de 1 bloco: executa Align_EndToEnd e MixAndRender
       - Se houver apenas 1 bloco: dispensa junção desnecessária
       - Aplica a macro escolhida na faixa consolidada
       - Exporta o áudio final único
    3. Se juntar=False:
       - Para cada bloco: limpa, importa, aplica macro, exporta
    4. Garante limpeza completa das faixas no final.
    """
    def report(msg):
        if progress_callback:
            progress_callback(msg)
        sys.stderr.write(f'[Audacity] {msg}\n')

    if not itens_audio:
        return {'success': False, 'error': 'Nenhum áudio fornecido para processamento.'}

    with AUDACITY_LOCK:
        os.makedirs(pasta_saida, exist_ok=True)
        report('Iniciando o Audacity...')
        launch_audacity()

        client = PipeClient()
        connected = client.connect(timeout=15.0)
        if not connected:
            close_audacity()
            return {'success': False, 'error': 'Não foi possível conectar ao Audacity via mod-script-pipe.'}

        try:
            # Carrega as linhas da macro, se fornecida
            linhas_macro = []
            if macro_path and os.path.isfile(macro_path):
                with open(macro_path, 'r', encoding='utf-8', errors='ignore') as f:
                    linhas_macro = [l.strip() for l in f.readlines() if l.strip() and not l.startswith('#')]
                # Garante que todo macro termine com o TruncateSilence de 1,3s / 30% como etapa final
                ultimo_cmd = linhas_macro[-1] if linhas_macro else ''
                if 'TruncateSilence' not in ultimo_cmd or 'Minimum="1,3"' not in ultimo_cmd:
                    linhas_macro.append(CMD_TRAVAR_SILENCIO_FINAL)
                report(f'Macro selecionada: {os.path.basename(macro_path)} ({len(linhas_macro)} comandos)')

            arquivos_gerados = []

            if juntar:
                # ════════════════════════════════════════════════════════
                # 1. JUNTAR PRIMEIRO (Ordem estrita requerida pelo usuário)
                # ════════════════════════════════════════════════════════
                report('Garantindo projeto limpo no Audacity...')
                limpar_todas_faixas(client)

                report(f'Importando {len(itens_audio)} faixa(s) para junção...')
                for idx, item in enumerate(itens_audio, 1):
                    caminho_norm = os.path.abspath(item['caminho_wav']).replace('\\', '/')
                    report(f'Importando bloco {idx}/{len(itens_audio)}: {item.get("nome", f"bloco_{idx}")}')
                    client.send(f'Import2: Filename="{caminho_norm}"', timeout=15.0)
                    time.sleep(0.15)

                if len(itens_audio) > 1:
                    report('Alinhando áudios de ponta a ponta (Align_EndToEnd)...')
                    client.send('SelectAll:', timeout=3.0)
                    client.send('Align_EndToEnd:', timeout=10.0)
                    time.sleep(0.3)

                    report('Renderizando em faixa única consolidada (MixAndRender)...')
                    client.send('SelectAll:', timeout=3.0)
                    client.send('MixAndRender:', timeout=15.0)
                    time.sleep(0.5)
                else:
                    report('Apenas 1 bloco enviado: alinhamento dispensado, aplicando efeitos diretamente...')

                # ════════════════════════════════════════════════════════
                # 2. APLICAR MACRO NA FAIXA ÚNICA
                # ════════════════════════════════════════════════════════
                if linhas_macro:
                    client.send('SelectAll:', timeout=3.0)
                    for l in linhas_macro:
                        nome_cmd = l.split(':')[0]
                        report(f'Aplicando efeito: {nome_cmd}...')
                        client.send(l, timeout=60.0)
                        time.sleep(0.2)

                # ════════════════════════════════════════════════════════
                # 3. EXPORTAR O ÁUDIO CONSOLIDADO
                # ════════════════════════════════════════════════════════
                nome_final = (itens_audio[0].get('nome_unificado') or 'audio_completo').replace('.wav', '').strip()
                caminho_saida = os.path.join(pasta_saida, f'{nome_final}.wav')
                saida_norm = os.path.abspath(caminho_saida).replace('\\', '/')

                report('Exportando áudio final masterizado...')
                client.send('SelectAll:', timeout=3.0)
                client.send(f'Export2: Filename="{saida_norm}" NumChannels=1', timeout=30.0)
                time.sleep(0.5)

                # Limpa projeto
                limpar_todas_faixas(client)

                if os.path.isfile(caminho_saida) and os.path.getsize(caminho_saida) > 1000:
                    arquivos_gerados.append({
                        'tipo': 'unificado',
                        'nome': nome_final,
                        'caminho': caminho_saida,
                        'tamanho': os.path.getsize(caminho_saida)
                    })
                else:
                    raise Exception('O arquivo unificado masterizado não foi gerado pelo Audacity.')

            else:
                # ════════════════════════════════════════════════════════
                # PROCESSAR BLOCO A BLOCO (Sem junção)
                # ════════════════════════════════════════════════════════
                for idx, item in enumerate(itens_audio, 1):
                    nome = item.get('nome', f'bloco_{idx}').replace('.wav', '').strip()
                    caminho_norm = os.path.abspath(item['caminho_wav']).replace('\\', '/')
                    caminho_saida = os.path.join(pasta_saida, f'{nome}.wav')
                    saida_norm = os.path.abspath(caminho_saida).replace('\\', '/')

                    report(f'Processando bloco {idx}/{len(itens_audio)}: {nome}...')
                    limpar_todas_faixas(client)
                    client.send(f'Import2: Filename="{caminho_norm}"', timeout=10.0)
                    time.sleep(0.2)

                    if linhas_macro:
                        client.send('SelectAll:', timeout=3.0)
                        for l in linhas_macro:
                            nome_cmd = l.split(':')[0]
                            client.send(l, timeout=45.0)
                            time.sleep(0.15)

                    client.send('SelectAll:', timeout=3.0)
                    client.send(f'Export2: Filename="{saida_norm}" NumChannels=1', timeout=20.0)
                    time.sleep(0.3)

                    limpar_todas_faixas(client)

                    if os.path.isfile(caminho_saida) and os.path.getsize(caminho_saida) > 1000:
                        arquivos_gerados.append({
                            'tipo': 'bloco',
                            'nome': nome,
                            'caminho': caminho_saida,
                            'tamanho': os.path.getsize(caminho_saida)
                        })

            report('Processamento no Audacity concluído!')
            return {'success': True, 'arquivos': arquivos_gerados}

        except Exception as e:
            report(f'Erro durante automação do Audacity: {e}')
            return {'success': False, 'error': str(e)}

        finally:
            # Garante que o projeto do Audacity fique 100% limpo para o próximo lote
            try:
                limpar_todas_faixas(client)
            except:
                pass
            # Fecha apenas a conexão dos pipes
            client.close()
            report('Projeto liberado. Audacity mantido em prontidão para novos lotes.')

def gerar_comando_truncate_silence(duracao="1,3", compressao="30", limiar="-35", descartar="0,5"):
    """Gera o comando TruncateSilence com os parâmetros informados, formatados para o Audacity."""
    dur_str = str(duracao).strip().replace('.', ',')
    comp_str = str(compressao).strip()
    lim_str = str(limiar).strip()
    desc_str = str(descartar).strip().replace('.', ',')
    return (
        f'TruncateSilence:Action="Compress Excess Silence" '
        f'Compress="{comp_str}" Independent="1" Minimum="{dur_str}" '
        f'Threshold="{lim_str}" Truncate="{desc_str}" '
        f'TruncateEnd="1" TruncateMiddle="1" TruncateStart="1"'
    )

def executar_travar_silencio(caminho_wav, duracao="1,3", compressao="30", limiar="-35", descartar="0,5", progress_callback=None):
    """
    Executa exclusivamente o corte de silêncio configurado
    diretamente no arquivo WAV informado, substituindo-o de forma atômica e segura.
    Protegido por AUDACITY_LOCK.
    """
    def report(msg):
        if progress_callback:
            progress_callback(msg)
        sys.stderr.write(f'[Audacity-TravarSilencio] {msg}\n')

    if not os.path.isfile(caminho_wav):
        return {'success': False, 'error': f'Arquivo WAV não encontrado: {caminho_wav}'}

    cmd_truncate = gerar_comando_truncate_silence(duracao=duracao, compressao=compressao, limiar=limiar, descartar=descartar)

    with AUDACITY_LOCK:
        report(f'Iniciando Audacity para travar silêncio ({duracao}s / {compressao}%)...')
        launch_audacity()

        client = PipeClient()
        connected = client.connect(timeout=15.0)
        if not connected:
            close_audacity()
            return {'success': False, 'error': 'Não foi possível conectar ao Audacity via mod-script-pipe.'}

        try:
            report('Garantindo projeto limpo no Audacity...')
            limpar_todas_faixas(client)

            caminho_norm = os.path.abspath(caminho_wav).replace('\\', '/')
            caminho_temp = os.path.abspath(caminho_wav + '.trunc_temp.wav').replace('\\', '/')

            report(f'Importando áudio para aplicar corte de silêncio: {os.path.basename(caminho_wav)}')
            client.send(f'Import2: Filename="{caminho_norm}"', timeout=15.0)
            time.sleep(0.2)

            report(f'Selecionando áudio e aplicando TruncateSilence ({duracao}s / {compressao}%)...')
            client.send('SelectAll:', timeout=3.0)
            client.send(cmd_truncate, timeout=45.0)
            time.sleep(0.2)

            report('Exportando áudio com silêncio ajustado...')
            client.send('SelectAll:', timeout=3.0)
            client.send(f'Export2: Filename="{caminho_temp}" NumChannels=1', timeout=30.0)
            time.sleep(0.3)

            # Limpa o projeto no Audacity antes de mover o arquivo para liberar locks do Windows
            limpar_todas_faixas(client)

            caminho_temp_os = os.path.abspath(caminho_wav + '.trunc_temp.wav')
            if os.path.isfile(caminho_temp_os) and os.path.getsize(caminho_temp_os) > 500:
                os.replace(caminho_temp_os, caminho_wav)
                report('Arquivo WAV atualizado com sucesso!')
                return {'success': True, 'caminho_wav': caminho_wav}
            else:
                raise Exception('O Audacity não gerou o áudio temporário truncado.')

        except Exception as e:
            report(f'Erro ao travar silêncio: {e}')
            return {'success': False, 'error': str(e)}

        finally:
            try:
                limpar_todas_faixas(client)
            except:
                pass
            client.close()
