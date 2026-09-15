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
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
    wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE
]
kernel32.WaitNamedPipeW.restype = wintypes.BOOL
kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
INVALID_HANDLE = wintypes.HANDLE(-1).value

TH32CS_SNAPPROCESS = 0x00000002

class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ('dwSize', wintypes.DWORD),
        ('cntUsage', wintypes.DWORD),
        ('th32ProcessID', wintypes.DWORD),
        ('th32DefaultHeapID', ctypes.c_void_p),
        ('th32ModuleID', wintypes.DWORD),
        ('cntThreads', wintypes.DWORD),
        ('th32ParentProcessID', wintypes.DWORD),
        ('pcPriClassBase', ctypes.c_long),
        ('dwFlags', wintypes.DWORD),
        ('szExeFile', ctypes.c_char * 260)
    ]

def get_audacity_pids():
    """Retorna conjunto de PIDs do Audacity via Win32 Toolhelp32Snapshot (zero subprocessos, zero janelas CMD, instantâneo)."""
    pids = set()
    try:
        hSnap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not hSnap or hSnap == -1:
            return pids
        try:
            pe = PROCESSENTRY32()
            pe.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if kernel32.Process32First(hSnap, ctypes.byref(pe)):
                while True:
                    name = pe.szExeFile.decode('latin-1', errors='ignore').lower()
                    if 'audacity' in name:
                        pids.add(pe.th32ProcessID)
                    if not kernel32.Process32Next(hSnap, ctypes.byref(pe)):
                        break
        finally:
            kernel32.CloseHandle(hSnap)
    except Exception:
        pass
    return pids

PIPE_TO = r'\\.\pipe\ToSrvPipe'
PIPE_FROM = r'\\.\pipe\FromSrvPipe'

AUDACITY_INICIADO_POR_NOS = False
AUDACITY_LOCK = threading.Lock()

# Configuração padrão de corte/compressão de silêncio excessivo (1,3s / 40%)
CMD_TRAVAR_SILENCIO_FINAL = 'TruncateSilence:Action="Compress Excess Silence" Compress="40" Independent="1" Minimum="1,3" Threshold="-35" Truncate="0,5" TruncateEnd="1" TruncateMiddle="1" TruncateStart="1"'

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
            time.sleep(0.4)
        except Exception:
            pass
    time.sleep(0.3)
    return False

_CACHED_AUDACITY_EXE = None

def find_audacity_exe():
    """
    Localiza dinamicamente o executável do Audacity no computador:
    1. Retorna cache em memória instantaneamente.
    2. Procura nas pastas de instalação padrão (%ProgramFiles%, C:, D:, etc.).
    3. Procura no PATH do sistema.
    4. Procura nas chaves de Registro do Windows.
    5. Se o processo já estiver rodando, descobre o caminho pelo sistema.
    """
    global _CACHED_AUDACITY_EXE
    if _CACHED_AUDACITY_EXE and os.path.isfile(_CACHED_AUDACITY_EXE):
        return _CACHED_AUDACITY_EXE

    # 1. Pastas padrão no Windows (instantâneo, sem criar subprocessos)
    locais_comuns = [
        r'C:\Program Files\Audacity\Audacity.exe',
        r'C:\Program Files (x86)\Audacity\Audacity.exe',
        os.path.expandvars(r'%ProgramFiles%\Audacity\Audacity.exe'),
        os.path.expandvars(r'%ProgramFiles(x86)%\Audacity\Audacity.exe'),
        os.path.expandvars(r'%LOCALAPPDATA%\Programs\Audacity\Audacity.exe'),
    ]
    for disco in ['C', 'D', 'E', 'F', 'G']:
        locais_comuns.append(f'{disco}:\\Audacity\\Audacity.exe')
        locais_comuns.append(f'{disco}:\\Program Files\\Audacity\\Audacity.exe')

    for p in locais_comuns:
        if os.path.isfile(p):
            _CACHED_AUDACITY_EXE = os.path.abspath(p)
            return _CACHED_AUDACITY_EXE

    # 2. PATH do Windows
    w = shutil.which('audacity') or shutil.which('Audacity')
    if w and os.path.isfile(w):
        _CACHED_AUDACITY_EXE = os.path.abspath(w)
        return _CACHED_AUDACITY_EXE

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
                                _CACHED_AUDACITY_EXE = os.path.abspath(val)
                                return _CACHED_AUDACITY_EXE
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
                                                    _CACHED_AUDACITY_EXE = os.path.abspath(caminho)
                                                    return _CACHED_AUDACITY_EXE
                                                cand = os.path.join(caminho, 'Audacity.exe')
                                                if os.path.isfile(cand):
                                                    _CACHED_AUDACITY_EXE = os.path.abspath(cand)
                                                    return _CACHED_AUDACITY_EXE
                                            except Exception:
                                                pass
                            except Exception:
                                pass
            except Exception:
                pass
    except Exception:
        pass

    # 4. Se já está rodando, descobre caminho do executável ativo via Win32 API direta
    try:
        pids = get_audacity_pids()
        for pid in pids:
            h = kernel32.OpenProcess(0x1000, False, pid)
            if h:
                try:
                    buf = ctypes.create_unicode_buffer(512)
                    size = wintypes.DWORD(512)
                    if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        p = buf.value
                        if p and os.path.isfile(p):
                            _CACHED_AUDACITY_EXE = os.path.abspath(p)
                            return _CACHED_AUDACITY_EXE
                finally:
                    kernel32.CloseHandle(h)
    except Exception:
        pass

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
    Remove arquivos temporários residuais de sessões do Audacity (.aup3unsaved*, AutoSave)
    que causam a exibição da janela modal 'Recuperação Automática de Falhas'.
    """
    try:
        pastas_sessao = [
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'audacity', 'SessionData'),
            os.path.join(os.environ.get('APPDATA', ''), 'audacity', 'SessionData'),
            os.path.join(os.environ.get('APPDATA', ''), 'audacity', 'AutoSave'),
        ]
        for session_dir in pastas_sessao:
            if os.path.isdir(session_dir):
                for f in glob.glob(os.path.join(session_dir, '*')):
                    for _ in range(3):
                        try:
                            if os.path.isfile(f):
                                os.remove(f)
                            break
                        except Exception:
                            time.sleep(0.05)
    except Exception:
        pass

def ensure_audacity_cfg():
    """Garante que o módulo de script pipe esteja ativo, telas de splash/intro/ajuda desativadas e janela configurada fora da tela."""
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

            # Desativa varredura lenta de plugins/efeitos na inicialização que exibe a modal "O Audacity está iniciando..."
            if '[Effects]' in content:
                if 'SkipEffectsScanAtStartup=0' in content:
                    content = content.replace('SkipEffectsScanAtStartup=0', 'SkipEffectsScanAtStartup=1')
                    changed = True
                elif 'SkipEffectsScanAtStartup=1' not in content:
                    content = content.replace('[Effects]', '[Effects]\nSkipEffectsScanAtStartup=1')
                    changed = True
            else:
                content += '\n[Effects]\nSkipEffectsScanAtStartup=1\n'
                changed = True

            # Desativa tela de splash inicial e diálogos de boas-vindas
            if '[GUI]' not in content:
                content += '\n[GUI]\nShowSplashScreen=0\nShowHowToGetHelpAtLaunch=0\nShowHelpAtLaunch=0\nIntroOrderStart=0\n'
                changed = True
            else:
                if 'ShowSplashScreen=1' in content:
                    content = content.replace('ShowSplashScreen=1', 'ShowSplashScreen=0')
                    changed = True
                elif 'ShowSplashScreen=0' not in content:
                    content = content.replace('[GUI]', '[GUI]\nShowSplashScreen=0')
                    changed = True

                if 'IntroOrderStart=1' in content:
                    content = content.replace('IntroOrderStart=1', 'IntroOrderStart=0')
                    changed = True
                elif 'IntroOrderStart=0' not in content:
                    content = content.replace('[GUI]', '[GUI]\nIntroOrderStart=0')
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

            # Coordenadas de inicialização sempre fora de qualquer monitor
            if '[Window]' not in content:
                content += '\n[Window]\nX=-32000\nY=-32000\nNormal_X=-32000\nNormal_Y=-32000\nWidth=1180\nHeight=714\nNormal_Width=1180\nNormal_Height=714\nMaximized=0\nIconized=0\n'
                changed = True
            else:
                if 'Iconized=1' in content:
                    content = content.replace('Iconized=1', 'Iconized=0')
                    changed = True
                if 'Maximized=1' in content:
                    content = content.replace('Maximized=1', 'Maximized=0')
                    changed = True

            if changed:
                with open(cfg_path, 'w', encoding='utf-8') as f:
                    f.write(content)
        except Exception as e:
            sys.stderr.write(f'[AudacityCfg] Erro ao verificar cfg: {e}\n')

def is_audacity_running():
    global _AUDACITY_PIDS
    if _AUDACITY_PIDS:
        vivos = set()
        for pid in list(_AUDACITY_PIDS):
            h = kernel32.OpenProcess(0x1000, False, pid)
            if h:
                code = wintypes.DWORD()
                if kernel32.GetExitCodeProcess(h, ctypes.byref(code)) and code.value == 259: # STILL_ACTIVE
                    vivos.add(pid)
                kernel32.CloseHandle(h)
        _AUDACITY_PIDS = vivos
        if _AUDACITY_PIDS:
            return True

    # Se há servidor do Audacity ativo escutando no pipe
    if kernel32.WaitNamedPipeW(PIPE_TO, 0):
        return True

    # Fallback instantâneo via Toolhelp32Snapshot (zero subprocessos, zero janelas CMD)
    pids = get_audacity_pids()
    if pids:
        _AUDACITY_PIDS.update(pids)
        return True
    return False

# Configurações de API do Windows (Win32 / Win64)
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL

SetWindowLongPtrW = getattr(user32, 'SetWindowLongPtrW', user32.SetWindowLongW)
SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
SetWindowLongPtrW.restype = ctypes.c_ssize_t

GetWindowLongPtrW = getattr(user32, 'GetWindowLongPtrW', user32.GetWindowLongW)
GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
GetWindowLongPtrW.restype = ctypes.c_ssize_t

user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL

user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
user32.SetWindowRgn.restype = ctypes.c_int

gdi32.CreateRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
gdi32.CreateRectRgn.restype = wintypes.HRGN

# APIs para enumeração no desktop interativo do usuário ('Default')
user32.OpenDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.OpenDesktopW.restype = wintypes.HDESK
user32.CloseDesktop.argtypes = [wintypes.HDESK]
user32.CloseDesktop.restype = wintypes.BOOL

EnumDesktopWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumDesktopWindows.argtypes = [wintypes.HDESK, EnumDesktopWindowsProc, wintypes.LPARAM]
user32.EnumDesktopWindows.restype = wintypes.BOOL

WINEVENTPROC = ctypes.WINFUNCTYPE(
    None,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.HWND,
    wintypes.LONG,
    wintypes.LONG,
    wintypes.DWORD,
    wintypes.DWORD
)
user32.SetWinEventHook.argtypes = [
    wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE,
    WINEVENTPROC, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD
]
user32.SetWinEventHook.restype = wintypes.HANDLE
user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
user32.UnhookWinEvent.restype = wintypes.BOOL

user32.CreateDesktopW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
user32.CreateDesktopW.restype = wintypes.HDESK

SILENT_DESKTOP_NAME = "AudacitySilentDesktop"
_h_silent_desktop = None

def _get_or_create_silent_desktop():
    global _h_silent_desktop
    if _h_silent_desktop:
        return _h_silent_desktop
    try:
        _h_silent_desktop = user32.CreateDesktopW(SILENT_DESKTOP_NAME, None, None, 0, 0x01FF, None)
    except Exception as e:
        sys.stderr.write(f"[Desktop] Falha ao criar desktop silencioso: {e}\n")
    return _h_silent_desktop

# ════════════════════════════════════════════════════════════════
# SILENCIADOR EM NÍVEL DE KERNEL/DWM DO AUDACITY
# Intercepta eventos do Windows e neutraliza instantaneamente
# qualquer janela (splash "O Audacity está iniciando...", diálogos de progresso, etc.)
# antes mesmo que um único pixel possa ser renderizado no monitor do usuário.
# ════════════════════════════════════════════════════════════════
SWP_FLAGS = 0x0010 | 0x0001 | 0x0004 | 0x0080 # SWP_HIDEWINDOW | NOACTIVATE | NOSIZE | NOZORDER
GWL_EXSTYLE = -20
GWL_STYLE = -16
WS_EX_LAYERED = 0x00080000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
LWA_ALPHA = 0x00000002

_SILENCER_ACTIVE = False
_SILENCER_THREAD = None
_SILENCER_HOOK = None
_SILENCER_HOOK_READY = threading.Event()
_AUDACITY_PID_CACHE = {}
_AUDACITY_PIDS = set()
_CB_HOLDER = None
_ENUM_DESK_PROC = None

def _atualizar_pids_audacity():
    """Mantém a lista de PIDs do Audacity atualizada sem subprocessos."""
    global _AUDACITY_PIDS
    pids = get_audacity_pids()
    if pids:
        _AUDACITY_PIDS.update(pids)

def _is_audacity_pid(pid):
    if not pid:
        return False
    if pid in _AUDACITY_PIDS:
        return True
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
            if res:
                _AUDACITY_PIDS.add(pid)
            return res
    finally:
        kernel32.CloseHandle(h)
    return False

def neutralizar_janela_audacity(hwnd):
    """
    Torna a janela 100% invisível em nível de kernel/DWM:
    1. Define Alpha = 0 (transparência total no DWM).
    2. Define região de recorte nula (0x0 pixels visíveis).
    3. Remove da Barra de Tarefas e do Alt-Tab (ToolWindow).
    4. Move para coordenadas fora de qualquer monitor (-32000, -32000) e executa SW_HIDE.
    """
    try:
        ex = GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        SetWindowLongPtrW(hwnd, GWL_EXSTYLE, (ex | WS_EX_LAYERED | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
        user32.SetLayeredWindowAttributes(hwnd, 0, 0, LWA_ALPHA)
        
        rgn = gdi32.CreateRectRgn(0, 0, 0, 0)
        user32.SetWindowRgn(hwnd, rgn, 1)

        user32.SetWindowPos(hwnd, 0, -32000, -32000, 0, 0, SWP_FLAGS)
        user32.ShowWindow(hwnd, 0)
        
        st = GetWindowLongPtrW(hwnd, GWL_STYLE)
        if st & 0x10000000: # WS_VISIBLE
            SetWindowLongPtrW(hwnd, GWL_STYLE, st & ~0x10000000)
    except Exception:
        pass

def _silencer_worker(target_pid=0):
    global _SILENCER_ACTIVE, _SILENCER_HOOK, _CB_HOLDER, _ENUM_DESK_PROC
    
    def _hook_cb(hWinEventHook, event, hwnd, idObject, idChild, dwEventThread, dwmsEventTime):
        if hwnd and idObject == 0:
            p = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
            pid = p.value
            if pid and (pid in _AUDACITY_PIDS or (target_pid > 0 and pid == target_pid) or _is_audacity_pid(pid)):
                _AUDACITY_PIDS.add(pid)
                neutralizar_janela_audacity(hwnd)

    _CB_HOLDER = WINEVENTPROC(_hook_cb)
    _SILENCER_HOOK = user32.SetWinEventHook(
        0x0001,
        0x7FFFFFFF,
        0,
        _CB_HOLDER,
        target_pid if target_pid > 0 else 0,
        0,
        0
    )

    def _desk_enum_cb(hwnd, lparam):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        pid = p.value
        if pid and (pid in _AUDACITY_PIDS or (target_pid > 0 and pid == target_pid) or _is_audacity_pid(pid)):
            _AUDACITY_PIDS.add(pid)
            neutralizar_janela_audacity(hwnd)
        return True

    _ENUM_DESK_PROC = EnumDesktopWindowsProc(_desk_enum_cb)
    h_desktop = user32.OpenDesktopW('Default', 0, False, 0x01FF)

    _SILENCER_HOOK_READY.set()
    ensure_audacity_hidden()

    msg = wintypes.MSG()
    while _SILENCER_ACTIVE:
        try:
            while user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))

            # Varredura direta e instantânea no Desktop Default sem nenhum subprocesso
            if h_desktop:
                user32.EnumDesktopWindows(h_desktop, _ENUM_DESK_PROC, 0)
        except Exception:
            pass
        time.sleep(0.002)

    if h_desktop:
        try:
            user32.CloseDesktop(h_desktop)
        except Exception:
            pass

    if _SILENCER_HOOK:
        try:
            user32.UnhookWinEvent(_SILENCER_HOOK)
        except Exception:
            pass
        _SILENCER_HOOK = None

def ensure_audacity_hidden():
    """Varredura imediata para forçar neutralização em qualquer janela existente do Audacity no Desktop interativo."""
    try:
        def enum_cb(hwnd, lparam):
            try:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                p = pid.value
                if p and (p in _AUDACITY_PIDS or _is_audacity_pid(p)):
                    _AUDACITY_PIDS.add(p)
                    neutralizar_janela_audacity(hwnd)
            except Exception:
                pass
            return True

        fn = EnumDesktopWindowsProc(enum_cb)
        h_desk = user32.OpenDesktopW('Default', 0, False, 0x01FF)
        if h_desk:
            user32.EnumDesktopWindows(h_desk, fn, 0)
            user32.CloseDesktop(h_desk)

        # Também varre a área de trabalho do processo atual
        user32.EnumWindows(fn, 0)
    except Exception:
        pass

def start_audacity_silencer(target_pid=0):
    """Inicia thread sentinela que intercepta e neutraliza instantaneamente qualquer tela do Audacity."""
    global _SILENCER_ACTIVE, _SILENCER_THREAD, _SILENCER_HOOK_READY
    if _SILENCER_ACTIVE and _SILENCER_HOOK:
        ensure_audacity_hidden()
        return
    
    stop_audacity_silencer()
    _SILENCER_HOOK_READY.clear()
    _SILENCER_ACTIVE = True
    _SILENCER_THREAD = threading.Thread(target=_silencer_worker, args=(target_pid,), daemon=True)
    _SILENCER_THREAD.start()
    # Aguarda o gancho estar 100% instalado e ativo no Windows antes de prosseguir
    _SILENCER_HOOK_READY.wait(timeout=1.0)

def stop_audacity_silencer():
    """Interrompe a thread sentinela."""
    global _SILENCER_ACTIVE, _SILENCER_THREAD
    _SILENCER_ACTIVE = False
    if _SILENCER_THREAD and _SILENCER_THREAD.is_alive():
        _SILENCER_THREAD.join(timeout=0.3)
    _SILENCER_THREAD = None

def minimize_audacity():
    """Garante que a janela do Audacity fique oculta em segundo plano."""
    start_audacity_silencer()
    ensure_audacity_hidden()

_IDLE_TIMER = None
_IDLE_LOCK = threading.Lock()

def _cancel_idle_close():
    """Cancela qualquer agendamento pendente de encerramento por inatividade."""
    global _IDLE_TIMER
    with _IDLE_LOCK:
        if _IDLE_TIMER is not None:
            try:
                _IDLE_TIMER.cancel()
            except Exception:
                pass
            _IDLE_TIMER = None

def _schedule_idle_close(timeout=180.0):
    """Agenda o encerramento automático do Audacity após um período de inatividade (padrão: 3 min)."""
    global _IDLE_TIMER
    with _IDLE_LOCK:
        if _IDLE_TIMER is not None:
            try:
                _IDLE_TIMER.cancel()
            except Exception:
                pass
        
        def _on_idle():
            sys.stderr.write(f'[Audacity] Inativo por {int(timeout)}s. Encerrando sessão de segundo plano...\n')
            close_audacity(force=True)

        _IDLE_TIMER = threading.Timer(timeout, _on_idle)
        _IDLE_TIMER.daemon = True
        _IDLE_TIMER.start()

def launch_audacity():
    """Inicia o Audacity 100% invisível em segundo plano (SW_HIDE), sem nenhuma intro, splash ou janela na tela.
    Retorna True se acabou de ser iniciado do zero, ou False se já estava aberto e foi reutilizado.
    """
    global AUDACITY_INICIADO_POR_NOS, _AUDACITY_PIDS

    _cancel_idle_close()

    audacity_exe = find_audacity_exe()
    if not audacity_exe:
        raise FileNotFoundError(
            'Audacity não foi encontrado no seu computador!\n'
            'Por favor, instale o Audacity pelo site oficial (https://www.audacityteam.org/download/) '
            'para que o estúdio possa masterizar seus áudios.'
        )

    if is_audacity_running():
        _atualizar_pids_audacity()
        sys.stderr.write('[Audacity] Audacity já em execução em segundo plano. Reutilizando sessão...\n')
        start_audacity_silencer()
        ensure_audacity_hidden()
        boost_audacity_priority()
        return False

    clean_audacity_sessions()
    ensure_audacity_cfg()

    AUDACITY_INICIADO_POR_NOS = True

    # 1. Ativa o sentinela interceptador no Windows ANTES de disparar o executável
    start_audacity_silencer()

    _get_or_create_silent_desktop()
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW | 0x00000004 # STARTF_USEPOSITION
    si.wShowWindow = 0 # SW_HIDE (100% invisível em background)
    si.dwX = -32000
    si.dwY = -32000
    si.lpDesktop = SILENT_DESKTOP_NAME

    proc = None
    CREATE_NO_WINDOW = 0x08000000
    try:
        proc = subprocess.Popen([audacity_exe], startupinfo=si, creationflags=CREATE_NO_WINDOW)
    except Exception:
        try:
            proc = subprocess.Popen([audacity_exe], creationflags=CREATE_NO_WINDOW)
        except Exception:
            proc = subprocess.Popen([audacity_exe])

    if proc and proc.pid:
        _AUDACITY_PIDS.add(proc.pid)

    # 2. Varredura imediata para suprimir a criação da janela antes do primeiro frame
    ensure_audacity_hidden()
    boost_audacity_priority()
    return True

def boost_audacity_priority():
    """Garante prioridade acima do normal para o Audacity e impede estrangulamento por EcoQoS do Windows (zero subprocessos)."""
    try:
        pids = list(_AUDACITY_PIDS) or list(get_audacity_pids())
        for pid in pids:
            h = kernel32.OpenProcess(0x0200 | 0x0400, False, pid) # PROCESS_SET_INFORMATION | PROCESS_QUERY_INFORMATION
            if h:
                try:
                    kernel32.SetPriorityClass(h, 0x00008000) # ABOVE_NORMAL_PRIORITY_CLASS
                finally:
                    kernel32.CloseHandle(h)
    except Exception:
        pass

def close_audacity(force=False):
    """
    Encerra o Audacity.
    Se force=True ou foi iniciado pelo estúdio, fecha educadamente via pipe e garante término do processo.
    """
    global AUDACITY_INICIADO_POR_NOS, _AUDACITY_PID_CACHE, _AUDACITY_PIDS
    _cancel_idle_close()
    stop_audacity_silencer()
    _AUDACITY_PID_CACHE.clear()
    _AUDACITY_PIDS.clear()

    if not is_audacity_running():
        AUDACITY_INICIADO_POR_NOS = False
        clean_audacity_sessions()
        return

    if not force and not AUDACITY_INICIADO_POR_NOS:
        sys.stderr.write('[Audacity] Preservando Audacity aberto (foi iniciado previamente pelo usuário).\n')
        return

    sys.stderr.write('[Audacity] Encerrando o Audacity em segundo plano...\n')

    # 1. Tentativa graciosa instantânea via comando 'Exit:' do mod-script-pipe (sem aguardar resposta de processo em terminação)
    try:
        c = PipeClient()
        if c.connect(timeout=1.0):
            c.send_no_wait('Exit:')
            c.close()
            time.sleep(0.3)
    except Exception:
        pass

    # 2. Termina processo caso ainda persista via Win32 TerminateProcess direto (zero subprocessos, zero janelas CMD)
    pids = get_audacity_pids()
    if pids:
        for pid in pids:
            h = kernel32.OpenProcess(0x0001, False, pid) # PROCESS_TERMINATE
            if h:
                try:
                    kernel32.TerminateProcess(h, 0)
                finally:
                    kernel32.CloseHandle(h)
        time.sleep(0.3)

    AUDACITY_INICIADO_POR_NOS = False
    clean_audacity_sessions()
    sys.stderr.write('[Audacity] Processo do Audacity finalizado com sucesso.\n')

class PipeClient:
    def __init__(self):
        self.h_to = None
        self.h_from = None

    def connect(self, timeout=12.0):
        start = time.time()
        while time.time() - start < timeout:
            kernel32.WaitNamedPipeW(PIPE_TO, 300)
            h_to = kernel32.CreateFileW(PIPE_TO, GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
            if h_to != INVALID_HANDLE:
                kernel32.WaitNamedPipeW(PIPE_FROM, 300)
                h_from = kernel32.CreateFileW(PIPE_FROM, GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
                if h_from != INVALID_HANDLE:
                    self.h_to = h_to
                    self.h_from = h_from
                    return True
                kernel32.CloseHandle(h_to)
            time.sleep(0.3)
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

    def send_no_wait(self, cmd):
        """Envia um comando pelo pipe sem bloquear aguardando resposta (ideal para comandos como 'Exit:')."""
        if not self.h_to or self.h_to == INVALID_HANDLE:
            return
        try:
            cmd_bytes = (cmd.strip() + '\n').encode('utf-8')
            written = wintypes.DWORD()
            kernel32.WriteFile(self.h_to, cmd_bytes, len(cmd_bytes), ctypes.byref(written), None)
        except Exception:
            pass

    def close(self):
        if self.h_to and self.h_to != INVALID_HANDLE:
            try: kernel32.CloseHandle(self.h_to)
            except: pass
            self.h_to = None
        if self.h_from and self.h_from != INVALID_HANDLE:
            try: kernel32.CloseHandle(self.h_from)
            except: pass
            self.h_from = None
        time.sleep(0.1)

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
        novo_inicio = launch_audacity()

        client = PipeClient()
        connected = client.connect(timeout=6.0 if not novo_inicio else 15.0)
        if not connected and not novo_inicio:
            report('Sessão anterior não respondeu ao pipe. Reiniciando Audacity do zero...')
            close_audacity(force=True)
            time.sleep(0.5)
            novo_inicio = launch_audacity()
            connected = client.connect(timeout=15.0)

        if not connected:
            close_audacity(force=True)
            return {'success': False, 'error': 'Não foi possível conectar ao Audacity via mod-script-pipe.'}

        # Aguarda estabilização da engine gráfica e de áudio do Audacity apenas na inicialização fria
        if novo_inicio:
            time.sleep(0.8)
        else:
            time.sleep(0.1)
        boost_audacity_priority()

        try:
            # Carrega as linhas da macro, se fornecida
            linhas_macro = []
            if macro_path and os.path.isfile(macro_path):
                with open(macro_path, 'r', encoding='utf-8', errors='ignore') as f:
                    linhas_macro = [l.strip() for l in f.readlines() if l.strip() and not l.startswith('#')]
                # Garante que qualquer TruncateSilence tenha Independent="1" para que faixas concorrentes não fiquem vinculadas
                for idx_m, l_m in enumerate(linhas_macro):
                    if 'TruncateSilence' in l_m and 'Independent=' not in l_m:
                        linhas_macro[idx_m] = l_m.rstrip() + ' Independent="1"'
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
                    client.send('SelectAll:', timeout=5.0)
                    client.send('Align_EndToEnd:', timeout=60.0)
                    time.sleep(0.3)

                    report('Renderizando em faixa única consolidada (MixAndRender)...')
                    client.send('SelectAll:', timeout=5.0)
                    client.send('MixAndRender:', timeout=180.0)
                    time.sleep(0.5)
                else:
                    report('Apenas 1 bloco enviado: alinhamento dispensado, aplicando efeitos diretamente...')

                # ════════════════════════════════════════════════════════
                # 2. APLICAR MACRO NA FAIXA ÚNICA
                # ════════════════════════════════════════════════════════
                if linhas_macro:
                    client.send('SelectAll:', timeout=5.0)
                    for l in linhas_macro:
                        nome_cmd = l.split(':')[0]
                        report(f'Aplicando efeito: {nome_cmd}...')
                        client.send(l, timeout=300.0)
                        time.sleep(0.2)

                # ════════════════════════════════════════════════════════
                # 3. EXPORTAR O ÁUDIO CONSOLIDADO
                # ════════════════════════════════════════════════════════
                nome_final = (itens_audio[0].get('nome_unificado') or itens_audio[0].get('nome') or 'audio_completo').replace('.wav', '').strip()
                caminho_saida = os.path.join(pasta_saida, f'{nome_final}.wav')
                saida_norm = os.path.abspath(caminho_saida).replace('\\', '/')

                report('Exportando áudio final masterizado...')
                client.send('SelectAll:', timeout=5.0)
                client.send(f'Export2: Filename="{saida_norm}" NumChannels=1', timeout=600.0)

                # Aguarda o Audacity concluir a gravação e o Windows liberar o arquivo no disco
                report('Aguardando gravação completa do arquivo no disco...')
                arquivo_pronto = False
                t0_espera = time.time()
                ultimo_tam = -1

                while time.time() - t0_espera < 60.0:
                    if os.path.isfile(caminho_saida):
                        tam_atual = os.path.getsize(caminho_saida)
                        if tam_atual > 1000:
                            if tam_atual == ultimo_tam:
                                arquivo_pronto = True
                                break
                            ultimo_tam = tam_atual
                    time.sleep(0.5)

                # Somente após o arquivo estar 100% gravado e liberado, limpa as faixas do projeto
                try:
                    limpar_todas_faixas(client)
                except:
                    pass

                if arquivo_pronto:
                    report(f'Áudio unificado masterizado com sucesso ({round(os.path.getsize(caminho_saida) / (1024 * 1024), 2)} MB)!')
                    arquivos_gerados.append({
                        'tipo': 'unificado',
                        'nome': nome_final,
                        'caminho': caminho_saida,
                        'tamanho': os.path.getsize(caminho_saida)
                    })
                else:
                    raise Exception(f'O arquivo unificado masterizado "{nome_final}.wav" não foi finalizado a tempo pelo Audacity.')

            else:
                # ════════════════════════════════════════════════════════
                # PROCESSAMENTO EM LOTE SIMULTÂNEO (Opção C: Masterização Unificada)
                # Todas as faixas são importadas simultaneamente para a mesma sessão do Audacity.
                # A macro (TDR Nova, LUFS -14dB, Compressor, Limiter) atua em todas em conjunto,
                # garantindo calibração de volume homogênea entre todos os arquivos/grupos.
                # Em seguida, cada faixa é selecionada individualmente (com Solo) e exportada para seu próprio arquivo.
                # ════════════════════════════════════════════════════════
                report('Garantindo projeto limpo no Audacity...')
                limpar_todas_faixas(client)
                time.sleep(0.4)

                report(f'Importando {len(itens_audio)} faixa(s) para masterização unificada no Audacity...')
                for idx, item in enumerate(itens_audio, 1):
                    nome = item.get('nome', f'bloco_{idx}').replace('.wav', '').strip()
                    caminho_norm = os.path.abspath(item['caminho_wav']).replace('\\', '/')
                    report(f'Importando faixa {idx}/{len(itens_audio)}: {nome}')
                    client.send(f'Import2: Filename="{caminho_norm}"', timeout=15.0)
                    time.sleep(0.25)

                if linhas_macro:
                    report(f'Aplicando macro unificada em todas as {len(itens_audio)} faixas simultaneamente...')
                    client.send('SelectAll:', timeout=5.0)
                    for l in linhas_macro:
                        nome_cmd = l.split(':')[0]
                        report(f'Aplicando efeito: {nome_cmd} em todas as faixas...')
                        client.send(l, timeout=300.0)
                        time.sleep(0.2)

                report(f'Exportando {len(itens_audio)} arquivos masterizados individualmente...')
                for idx, item in enumerate(itens_audio):
                    nome = item.get('nome', f'bloco_{idx+1}').replace('.wav', '').strip()
                    caminho_saida = os.path.join(pasta_saida, f'{nome}.wav')
                    saida_norm = os.path.abspath(caminho_saida).replace('\\', '/')

                    report(f'Exportando faixa {idx+1}/{len(itens_audio)}: {nome}.wav...')
                    # Seleciona estritamente a faixa atual e ativa Solo para isolamento total sem mixagem
                    client.send(f'SelectTracks: Mode="Set" Track="{idx}" TrackCount="1"', timeout=5.0)
                    client.send('SetTrackAudio: Solo="1"', timeout=5.0)
                    client.send(f'Export2: Filename="{saida_norm}" NumChannels=1', timeout=300.0)
                    client.send('SetTrackAudio: Solo="0"', timeout=5.0)

                    # Aguarda gravação completa do arquivo no disco
                    arquivo_bloco_pronto = False
                    t0_bloco = time.time()
                    ult_tam_b = -1
                    while time.time() - t0_bloco < 45.0:
                        if os.path.isfile(caminho_saida):
                            tam_b = os.path.getsize(caminho_saida)
                            if tam_b > 1000:
                                if tam_b == ult_tam_b:
                                    arquivo_bloco_pronto = True
                                    break
                                ult_tam_b = tam_b
                        time.sleep(0.3)

                    if arquivo_bloco_pronto:
                        arquivos_gerados.append({
                            'tipo': item.get('tipo', 'bloco'),
                            'nome': nome,
                            'caminho': caminho_saida,
                            'tamanho': os.path.getsize(caminho_saida)
                        })
                    else:
                        report(f'Aviso: O arquivo "{nome}.wav" não foi finalizado a tempo pelo Audacity.')

                try:
                    limpar_todas_faixas(client)
                except:
                    pass

            report('Processamento no Audacity concluído!')
            return {'success': True, 'arquivos': arquivos_gerados}

        except Exception as e:
            report(f'Erro durante automação do Audacity: {e}')
            return {'success': False, 'error': str(e)}

        finally:
            # Garante que o projeto do Audacity fique 100% limpo
            try:
                limpar_todas_faixas(client)
            except:
                pass
            try:
                client.close()
            except:
                pass
            # Mantém o Audacity em background para os próximos blocos e agenda encerramento após 3 min de inatividade
            _schedule_idle_close(180.0)
            report('Áudio concluído no Audacity. Sessão preservada em background.')

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
    def report(msg, pct=None):
        if progress_callback:
            try:
                progress_callback(msg, pct=pct)
            except TypeError:
                progress_callback(msg)
        sys.stderr.write(f'[Audacity-TravarSilencio] ({pct}%) {msg}\n')

    if not os.path.isfile(caminho_wav):
        return {'success': False, 'error': f'Arquivo WAV não encontrado: {caminho_wav}'}

    cmd_truncate = gerar_comando_truncate_silence(duracao=duracao, compressao=compressao, limiar=limiar, descartar=descartar)

    with AUDACITY_LOCK:
        report(f'Iniciando Audacity para travar silêncio ({duracao}s / {compressao}%)...', pct=10)
        novo_inicio = launch_audacity()

        client = PipeClient()
        connected = client.connect(timeout=6.0 if not novo_inicio else 15.0)
        if not connected and not novo_inicio:
            report('Sessão anterior não respondeu ao pipe. Reiniciando Audacity do zero...', pct=15)
            close_audacity(force=True)
            time.sleep(0.5)
            novo_inicio = launch_audacity()
            connected = client.connect(timeout=15.0)

        if not connected:
            close_audacity(force=True)
            return {'success': False, 'error': 'Não foi possível conectar ao Audacity via mod-script-pipe.'}

        if novo_inicio:
            time.sleep(0.5)
        else:
            time.sleep(0.1)
        boost_audacity_priority()

        try:
            report('Garantindo projeto limpo no Audacity...', pct=20)
            limpar_todas_faixas(client)
            time.sleep(0.4)

            caminho_norm = os.path.abspath(caminho_wav).replace('\\', '/')
            caminho_temp = os.path.abspath(caminho_wav + '.trunc_temp.wav').replace('\\', '/')

            report(f'Importando áudio para aplicar corte de silêncio: {os.path.basename(caminho_wav)}', pct=30)
            client.send(f'Import2: Filename="{caminho_norm}"', timeout=30.0)
            time.sleep(0.2)

            report(f'Aplicando TruncateSilence ({duracao}s / {compressao}%)...', pct=50)
            client.send('SelectAll:', timeout=3.0)
            client.send(cmd_truncate, timeout=300.0)
            time.sleep(0.2)

            report('Exportando áudio com silêncio ajustado...', pct=80)
            client.send('SelectAll:', timeout=3.0)
            client.send(f'Export2: Filename="{caminho_temp}" NumChannels=1', timeout=240.0)
            time.sleep(0.3)

            # Aguarda o Audacity concluir a gravação do arquivo temporário no disco
            caminho_temp_os = os.path.abspath(caminho_wav + '.trunc_temp.wav')
            arquivo_temp_pronto = False
            t0_trunc = time.time()
            ult_tam_tr = -1
            while time.time() - t0_trunc < 60.0:
                if os.path.isfile(caminho_temp_os):
                    tam_tr = os.path.getsize(caminho_temp_os)
                    if tam_tr > 500:
                        if tam_tr == ult_tam_tr:
                            arquivo_temp_pronto = True
                            break
                        ult_tam_tr = tam_tr
                time.sleep(0.15)

            # Limpa o projeto no Audacity antes de mover o arquivo para liberar locks do Windows
            try:
                limpar_todas_faixas(client)
                time.sleep(0.4)
            except:
                pass

            if arquivo_temp_pronto and os.path.isfile(caminho_temp_os) and os.path.getsize(caminho_temp_os) > 500:
                substituido = False
                for _ in range(15):
                    try:
                        os.replace(caminho_temp_os, caminho_wav)
                        substituido = True
                        break
                    except Exception:
                        time.sleep(0.2)
                if not substituido:
                    try:
                        shutil.copy2(caminho_temp_os, caminho_wav)
                        os.remove(caminho_temp_os)
                        substituido = True
                    except Exception:
                        pass

                report('Arquivo WAV atualizado com sucesso!', pct=100)
                return {'success': True, 'caminho_wav': caminho_wav}
            else:
                raise Exception('O Audacity não gerou o áudio temporário truncado a tempo.')

        except Exception as e:
            report(f'Erro ao travar silêncio: {e}')
            return {'success': False, 'error': str(e)}

        finally:
            try:
                limpar_todas_faixas(client)
            except:
                pass
            try:
                client.close()
            except:
                pass
            _schedule_idle_close(180.0)
            report('Silêncio ajustado no Audacity. Sessão preservada em background.')

def executar_travar_silencio_lote(itens, duracao="2", compressao="50", limiar="-35", descartar="0,5", progress_callback=None):
    """
    Executa o corte de silêncio configurado em LOTE para múltiplos arquivos WAV
    em uma ÚNICA sessão do Audacity, sem reiniciar o processo entre os arquivos.
    Protegido por AUDACITY_LOCK.
    """
    def report(msg, pct=None):
        if progress_callback:
            try:
                progress_callback(msg, pct=pct)
            except TypeError:
                progress_callback(msg)
        sys.stderr.write(f'[Audacity-TravarSilencioLote] ({pct}%) {msg}\n')

    if not itens:
        return {'success': False, 'error': 'Nenhum item informado para corte de silêncio.'}

    cmd_truncate = gerar_comando_truncate_silence(duracao=duracao, compressao=compressao, limiar=limiar, descartar=descartar)

    with AUDACITY_LOCK:
        report(f'Iniciando Audacity em segundo plano para travar silêncio em lote ({len(itens)} áudio(s))...', pct=5)
        novo_inicio = launch_audacity()

        client = PipeClient()
        connected = client.connect(timeout=6.0 if not novo_inicio else 15.0)
        if not connected and not novo_inicio:
            report('Sessão anterior não respondeu ao pipe. Reiniciando Audacity do zero...', pct=10)
            close_audacity(force=True)
            time.sleep(0.5)
            novo_inicio = launch_audacity()
            connected = client.connect(timeout=15.0)

        if not connected:
            close_audacity(force=True)
            return {'success': False, 'error': 'Não foi possível conectar ao Audacity via mod-script-pipe.'}

        if novo_inicio:
            time.sleep(0.5)
        else:
            time.sleep(0.1)
        boost_audacity_priority()

        resultados = []
        try:
            total = len(itens)
            report(f'Garantindo projeto limpo no Audacity...', pct=10)
            limpar_todas_faixas(client)
            time.sleep(0.4)

            for idx, item in enumerate(itens, 1):
                caminho_wav = item.get('caminhoWav') or item.get('caminho') or ''
                nome = item.get('nome') or os.path.basename(caminho_wav)
                if not os.path.isfile(caminho_wav):
                    report(f'Arquivo WAV {idx}/{total} não encontrado: {caminho_wav}')
                    continue

                pct_item_inicio = int(((idx - 1) / total) * 90) + 5
                report(f'Cortando silêncio {idx}/{total}: {nome} ({duracao}s / {compressao}%)...', pct=pct_item_inicio)

                # Limpa faixas antes de cada arquivo para garantir isolamento absoluto
                limpar_todas_faixas(client)
                time.sleep(0.35)

                caminho_norm = os.path.abspath(caminho_wav).replace('\\', '/')
                caminho_temp = os.path.abspath(caminho_wav + '.trunc_temp.wav').replace('\\', '/')

                client.send(f'Import2: Filename="{caminho_norm}"', timeout=30.0)
                time.sleep(0.25)

                client.send('SelectAll:', timeout=3.0)
                client.send(cmd_truncate, timeout=300.0)
                time.sleep(0.2)

                client.send('SelectAll:', timeout=3.0)
                client.send(f'Export2: Filename="{caminho_temp}" NumChannels=1', timeout=240.0)
                time.sleep(0.3)

                # Aguarda o Audacity concluir a gravação do arquivo temporário no disco
                caminho_temp_os = os.path.abspath(caminho_wav + '.trunc_temp.wav')
                arquivo_temp_pronto = False
                t0_trunc = time.time()
                ult_tam_tr = -1
                while time.time() - t0_trunc < 60.0:
                    if os.path.isfile(caminho_temp_os):
                        tam_tr = os.path.getsize(caminho_temp_os)
                        if tam_tr > 500:
                            if tam_tr == ult_tam_tr:
                                arquivo_temp_pronto = True
                                break
                            ult_tam_tr = tam_tr
                    time.sleep(0.15)

                try:
                    limpar_todas_faixas(client)
                    time.sleep(0.4)
                except:
                    pass

                if arquivo_temp_pronto and os.path.isfile(caminho_temp_os) and os.path.getsize(caminho_temp_os) > 500:
                    substituido = False
                    for _ in range(15):
                        try:
                            os.replace(caminho_temp_os, caminho_wav)
                            substituido = True
                            break
                        except Exception:
                            time.sleep(0.2)
                    if not substituido:
                        try:
                            shutil.copy2(caminho_temp_os, caminho_wav)
                            os.remove(caminho_temp_os)
                            substituido = True
                        except Exception:
                            pass

                    pct_item_concluido = int((idx / total) * 90) + 5
                    report(f'Áudio {idx}/{total} concluído: {nome}', pct=pct_item_concluido)
                    resultados.append({
                        'success': True,
                        'caminhoWav': caminho_wav,
                        'nome': nome,
                        'textoReferencia': item.get('textoReferencia') or item.get('texto', ''),
                        'gerarSrt': item.get('gerarSrt', True)
                    })
                else:
                    resultados.append({
                        'success': False,
                        'caminhoWav': caminho_wav,
                        'nome': nome,
                        'error': 'Falha ao exportar áudio truncado no Audacity'
                    })

            report('Todos os áudios foram processados com sucesso no Audacity!', pct=100)
            return {'success': True, 'itens': resultados}

        except Exception as e:
            report(f'Erro no processamento em lote: {e}')
            return {'success': False, 'error': str(e), 'itens': resultados}

        finally:
            try:
                limpar_todas_faixas(client)
            except:
                pass
            try:
                client.close()
            except:
                pass
            _schedule_idle_close(180.0)
            report('Sessão em lote do Audacity finalizada com sucesso.')

