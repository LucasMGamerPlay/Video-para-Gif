# -*- coding: utf-8 -*-
"""
Vídeo para GIF
Conversor de vídeo para GIF com interface gráfica (Tkinter).

Usa ffmpeg em duas passagens (palettegen + paletteuse), o que produz
GIFs com qualidade muito superior à conversão direta.

Requisitos: ffmpeg no PATH ou o pacote Python "imageio-ffmpeg".
"""

import os
import re
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_NOME = "Vídeo para GIF"

# Evita abrir janelas de console no Windows a cada chamada do ffmpeg
if os.name == "nt":
    _CREATE_NO_WINDOW = 0x08000000
    _STARTUPINFO = subprocess.STARTUPINFO()
    _STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
else:
    _CREATE_NO_WINDOW = 0
    _STARTUPINFO = None

# Presets de qualidade: cores da paleta, algoritmo de dithering,
# modo de análise da paleta e largura sugerida (0 = original).
QUALIDADES = {
    "Baixa (arquivo menor)": {
        "cores": 48,
        "dither": "bayer:bayer_scale=5",
        "stats": "diff",
        "largura": 320,
    },
    "Média (equilibrada)": {
        "cores": 128,
        "dither": "bayer:bayer_scale=3",
        "stats": "diff",
        "largura": 480,
    },
    "Alta": {
        "cores": 256,
        "dither": "sierra2_4a",
        "stats": "full",
        "largura": 640,
    },
    "Máxima (arquivo maior)": {
        "cores": 256,
        "dither": "sierra2_4a",
        "stats": "full",
        "largura": 0,
    },
}

LARGURAS = ["Original", "240", "320", "480", "640", "800", "1024", "1280"]

VIDEO_EXTS = [
    ("Vídeos", "*.mp4 *.mkv *.mov *.avi *.webm *.wmv *.flv *.m4v *.mpg *.mpeg *.ts *.3gp"),
    ("Todos os arquivos", "*.*"),
]

_RE_DURACAO = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d(?:\.\d+)?)")
_RE_RESOLUCAO = re.compile(r"Video:.*?[\s,](\d{2,5})x(\d{2,5})[\s,]")
_RE_FPS = re.compile(r"(\d+(?:\.\d+)?)\s+fps")


def localizar_ffmpeg():
    """Retorna o caminho do ffmpeg (PATH ou binário do imageio-ffmpeg)."""
    caminho = shutil.which("ffmpeg")
    if caminho:
        return caminho
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def formatar_tempo(segundos):
    if segundos is None or segundos < 0:
        return "--:--"
    m, s = divmod(int(segundos), 60)
    h, m = divmod(m, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


def formatar_tamanho(tamanho):
    valor = float(tamanho)
    for unidade in ("B", "KB", "MB", "GB"):
        if valor < 1024 or unidade == "GB":
            return "%.1f %s" % (valor, unidade)
        valor /= 1024.0


def parse_tempo(texto):
    """Aceita '12', '1:23', '00:01:23.5'. Retorna segundos ou None."""
    texto = (texto or "").strip()
    if not texto:
        return None
    partes = texto.split(":")
    try:
        partes = [float(p.replace(",", ".")) for p in partes]
    except ValueError:
        raise ValueError("Tempo inválido: %s" % texto)
    if len(partes) == 1:
        total = partes[0]
    elif len(partes) == 2:
        total = partes[0] * 60 + partes[1]
    elif len(partes) == 3:
        total = partes[0] * 3600 + partes[1] * 60 + partes[2]
    else:
        raise ValueError("Tempo inválido: %s" % texto)
    if total < 0:
        raise ValueError("Tempo negativo: %s" % texto)
    return total


class InfoVideo(object):
    def __init__(self, duracao=None, largura=None, altura=None, fps=None):
        self.duracao = duracao
        self.largura = largura
        self.altura = altura
        self.fps = fps

    def resumo(self):
        partes = []
        if self.largura and self.altura:
            partes.append("%dx%d" % (self.largura, self.altura))
        if self.duracao:
            partes.append(formatar_tempo(self.duracao))
        if self.fps:
            partes.append("%.4g fps" % self.fps)
        return "   |   ".join(partes) if partes else "sem informações"


class Conversor(object):
    """Executa o ffmpeg em duas passagens e reporta o progresso."""

    def __init__(self, ffmpeg, fila):
        self.ffmpeg = ffmpeg
        self.fila = fila
        self.proc = None
        self.cancelado = False

    # ------------------------------------------------------------------ util
    def _log(self, msg):
        self.fila.put(("log", msg))

    def _progresso(self, valor):
        self.fila.put(("progresso", max(0.0, min(100.0, valor))))

    def cancelar(self):
        self.cancelado = True
        proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass

    def inspecionar(self, caminho):
        """Lê duração/resolução/fps do vídeo usando 'ffmpeg -i'."""
        proc = subprocess.run(
            [self.ffmpeg, "-hide_banner", "-i", caminho],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            startupinfo=_STARTUPINFO,
            creationflags=_CREATE_NO_WINDOW,
        )
        saida = proc.stdout.decode("utf-8", "replace")
        info = InfoVideo()
        m = _RE_DURACAO.search(saida)
        if m:
            info.duracao = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
        m = _RE_RESOLUCAO.search(saida)
        if m:
            info.largura, info.altura = int(m.group(1)), int(m.group(2))
        m = _RE_FPS.search(saida)
        if m:
            info.fps = float(m.group(1))
        if info.duracao is None and info.largura is None:
            raise RuntimeError("Não foi possível ler o vídeo. Formato não suportado?")
        return info

    # ---------------------------------------------------------------- ffmpeg
    def _rodar(self, args, duracao_total, base, peso):
        """Roda o ffmpeg lendo -progress e atualizando a barra."""
        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=_STARTUPINFO,
            creationflags=_CREATE_NO_WINDOW,
        )
        erros = []
        t_erro = threading.Thread(
            target=lambda: erros.append(self.proc.stderr.read().decode("utf-8", "replace")),
            daemon=True,
        )
        t_erro.start()

        for linha in self.proc.stdout:
            linha = linha.decode("utf-8", "replace").strip()
            if linha.startswith("out_time_us=") or linha.startswith("out_time_ms="):
                try:
                    microssegundos = float(linha.split("=", 1)[1])
                except ValueError:
                    continue
                if duracao_total:
                    fracao = (microssegundos / 1_000_000.0) / duracao_total
                    self._progresso(base + peso * 100 * min(1.0, max(0.0, fracao)))

        codigo = self.proc.wait()
        t_erro.join(timeout=2)
        detalhe = erros[0] if erros else ""
        if self.cancelado:
            raise RuntimeError("cancelado")
        if codigo != 0:
            ultimas = [l for l in detalhe.strip().splitlines() if l.strip()][-4:]
            raise RuntimeError("ffmpeg falhou (código %s):\n%s" % (codigo, "\n".join(ultimas)))
        self._progresso(base + peso * 100)
        return detalhe

    def converter(self, opcoes):
        entrada = opcoes["entrada"]
        saida = opcoes["saida"]
        fps = opcoes["fps"]
        qualidade = QUALIDADES[opcoes["qualidade"]]
        largura = opcoes["largura"]
        inicio = opcoes["inicio"]
        duracao = opcoes["duracao"]
        loop = opcoes["loop"]

        info = self.inspecionar(entrada)
        self._log("Entrada: %s" % os.path.basename(entrada))
        self._log("Vídeo:   %s" % info.resumo())

        # quanto do vídeo será realmente processado (para a barra de progresso)
        total = info.duracao or 0
        if total and inicio:
            total = max(0.0, total - inicio)
        if duracao:
            total = min(total, duracao) if total else duracao

        cadeia = ["fps=%s" % fps]
        if largura:
            cadeia.append("scale=%d:-1:flags=lanczos" % largura)
        filtros = ",".join(cadeia)

        corte = []
        if inicio:
            corte += ["-ss", "%.3f" % inicio]
        if duracao:
            corte += ["-t", "%.3f" % duracao]

        pasta_tmp = tempfile.mkdtemp(prefix="video2gif_")
        paleta = os.path.join(pasta_tmp, "paleta.png")
        try:
            # ---- passagem 1: gerar a paleta de cores otimizada
            self._log("")
            self._log("[1/2] Gerando paleta de cores (%d cores, stats=%s)..."
                      % (qualidade["cores"], qualidade["stats"]))
            self._rodar(
                [self.ffmpeg, "-hide_banner", "-nostdin", "-y"]
                + corte
                + [
                    "-i", entrada,
                    "-vf", "%s,palettegen=max_colors=%d:stats_mode=%s"
                           % (filtros, qualidade["cores"], qualidade["stats"]),
                    "-progress", "pipe:1", "-nostats",
                    paleta,
                ],
                total, 0.0, 0.35,
            )

            # ---- passagem 2: aplicar a paleta e escrever o GIF
            self._log("[2/2] Convertendo para GIF (%s fps, dither=%s)..."
                      % (fps, qualidade["dither"].split(":")[0]))
            self._rodar(
                [self.ffmpeg, "-hide_banner", "-nostdin", "-y"]
                + corte
                + [
                    "-i", entrada,
                    "-i", paleta,
                    "-lavfi", "%s[x];[x][1:v]paletteuse=dither=%s"
                              % (filtros, qualidade["dither"]),
                    "-loop", "0" if loop else "-1",
                    "-progress", "pipe:1", "-nostats",
                    saida,
                ],
                total, 35.0, 0.65,
            )
        except BaseException:
            # não deixa GIF pela metade no disco se cancelar ou der erro
            if os.path.exists(saida):
                try:
                    os.remove(saida)
                except OSError:
                    pass
            raise
        finally:
            shutil.rmtree(pasta_tmp, ignore_errors=True)

        self._progresso(100)
        return os.path.getsize(saida)


class Aplicacao(ttk.Frame):
    def __init__(self, master, ffmpeg):
        ttk.Frame.__init__(self, master, padding=12)
        self.grid(row=0, column=0, sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self.ffmpeg = ffmpeg
        self.fila = queue.Queue()
        self.conversor = None
        self.thread = None
        self.info = None
        self.saida_final = None

        self.var_entrada = tk.StringVar()
        self.var_saida = tk.StringVar()
        self.var_qualidade = tk.StringVar(value="Alta")
        self.var_largura = tk.StringVar(value="640")
        self.var_fps = tk.IntVar(value=15)
        self.var_inicio = tk.StringVar()
        self.var_duracao = tk.StringVar()
        self.var_loop = tk.BooleanVar(value=True)
        self.var_status = tk.StringVar(value="Pronto. Selecione um vídeo.")
        self.var_info = tk.StringVar(value="Nenhum vídeo selecionado")

        self._montar()
        self.after(100, self._drenar_fila)

    # ------------------------------------------------------------- interface
    def _montar(self):
        linha = 0

        # ---- Arquivos
        cx = ttk.LabelFrame(self, text=" Arquivos ", padding=10)
        cx.grid(row=linha, column=0, sticky="ew")
        cx.columnconfigure(1, weight=1)

        ttk.Label(cx, text="Vídeo de entrada:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(cx, textvariable=self.var_entrada).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(cx, text="Procurar...", command=self.escolher_entrada).grid(row=0, column=2)

        ttk.Label(cx, text="GIF de saída:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(cx, textvariable=self.var_saida).grid(row=1, column=1, sticky="ew", padx=8)
        ttk.Button(cx, text="Salvar como...", command=self.escolher_saida).grid(row=1, column=2)

        ttk.Label(cx, textvariable=self.var_info, foreground="#2f6fae").grid(
            row=2, column=1, columnspan=2, sticky="w", padx=8, pady=(6, 0))

        # ---- Qualidade / FPS
        linha += 1
        op = ttk.LabelFrame(self, text=" Qualidade e taxa de quadros ", padding=10)
        op.grid(row=linha, column=0, sticky="ew", pady=(10, 0))
        op.columnconfigure(2, weight=1)

        ttk.Label(op, text="Qualidade:").grid(row=0, column=0, sticky="w", pady=4)
        combo = ttk.Combobox(op, textvariable=self.var_qualidade, state="readonly",
                             values=list(QUALIDADES.keys()), width=24)
        combo.grid(row=0, column=1, sticky="w", padx=8)
        combo.bind("<<ComboboxSelected>>", self._qualidade_mudou)
        ttk.Label(op, text="define o número de cores e o dithering da paleta",
                  foreground="#777").grid(row=0, column=2, sticky="w")

        ttk.Label(op, text="Largura (px):").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Combobox(op, textvariable=self.var_largura, values=LARGURAS,
                     width=12).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Label(op, text='"Original" mantém o tamanho do vídeo; a altura é proporcional',
                  foreground="#777").grid(row=1, column=2, sticky="w")

        ttk.Label(op, text="FPS:").grid(row=2, column=0, sticky="w", pady=4)
        quadro_fps = ttk.Frame(op)
        quadro_fps.grid(row=2, column=1, columnspan=2, sticky="ew", padx=8)
        quadro_fps.columnconfigure(0, weight=1)
        self.escala_fps = ttk.Scale(quadro_fps, from_=1, to=50, orient="horizontal",
                                    command=self._escala_mudou)
        self.escala_fps.set(15)
        self.escala_fps.grid(row=0, column=0, sticky="ew")
        ttk.Spinbox(quadro_fps, from_=1, to=50, width=5, textvariable=self.var_fps,
                    command=self._spin_mudou).grid(row=0, column=1, padx=(10, 4))
        ttk.Label(quadro_fps, text="quadros/s").grid(row=0, column=2)

        # ---- Recorte
        linha += 1
        rc = ttk.LabelFrame(self, text=" Recorte (opcional) ", padding=10)
        rc.grid(row=linha, column=0, sticky="ew", pady=(10, 0))

        ttk.Label(rc, text="Início:").grid(row=0, column=0, sticky="w")
        ttk.Entry(rc, textvariable=self.var_inicio, width=12).grid(row=0, column=1, padx=(8, 20))
        ttk.Label(rc, text="Duração:").grid(row=0, column=2, sticky="w")
        ttk.Entry(rc, textvariable=self.var_duracao, width=12).grid(row=0, column=3, padx=8)
        ttk.Label(rc, text="formato  mm:ss  ou  hh:mm:ss  (vazio = vídeo inteiro)",
                  foreground="#777").grid(row=0, column=4, sticky="w", padx=(10, 0))
        ttk.Checkbutton(rc, text="Repetir em loop infinito", variable=self.var_loop).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        # ---- Ações
        linha += 1
        ac = ttk.Frame(self)
        ac.grid(row=linha, column=0, sticky="ew", pady=(12, 0))
        ac.columnconfigure(0, weight=1)

        self.barra = ttk.Progressbar(ac, mode="determinate", maximum=100)
        self.barra.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        self.btn_converter = ttk.Button(ac, text="Converter", command=self.iniciar)
        self.btn_converter.grid(row=0, column=1)
        self.btn_cancelar = ttk.Button(ac, text="Cancelar", command=self.cancelar, state="disabled")
        self.btn_cancelar.grid(row=0, column=2, padx=6)
        self.btn_pasta = ttk.Button(ac, text="Abrir pasta", command=self.abrir_pasta, state="disabled")
        self.btn_pasta.grid(row=0, column=3)

        linha += 1
        ttk.Label(self, textvariable=self.var_status).grid(row=linha, column=0, sticky="w", pady=(8, 0))

        # ---- Registro
        linha += 1
        lg = ttk.LabelFrame(self, text=" Registro ", padding=6)
        lg.grid(row=linha, column=0, sticky="nsew", pady=(8, 0))
        lg.columnconfigure(0, weight=1)
        lg.rowconfigure(0, weight=1)
        self.rowconfigure(linha, weight=1)

        self.texto = tk.Text(lg, height=9, wrap="word", state="disabled",
                             background="#1e1e1e", foreground="#d4d4d4",
                             insertbackground="#d4d4d4", relief="flat",
                             font=("Consolas", 9))
        self.texto.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lg, orient="vertical", command=self.texto.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.texto.configure(yscrollcommand=sb.set)

        self.log("ffmpeg: %s" % self.ffmpeg)

    # ---------------------------------------------------------------- helpers
    def log(self, msg):
        self.texto.configure(state="normal")
        self.texto.insert("end", msg + "\n")
        self.texto.see("end")
        self.texto.configure(state="disabled")

    def _escala_mudou(self, valor):
        self.var_fps.set(int(float(valor)))

    def _spin_mudou(self):
        try:
            self.escala_fps.set(self.var_fps.get())
        except tk.TclError:
            pass

    def _qualidade_mudou(self, _evento=None):
        preset = QUALIDADES[self.var_qualidade.get()]
        self.var_largura.set("Original" if preset["largura"] == 0 else str(preset["largura"]))

    def escolher_entrada(self):
        caminho = filedialog.askopenfilename(title="Escolha o vídeo", filetypes=VIDEO_EXTS)
        if caminho:
            self.definir_entrada(caminho)

    def definir_entrada(self, caminho):
        self.var_entrada.set(caminho)
        self.var_saida.set(os.path.splitext(caminho)[0] + ".gif")
        try:
            info = Conversor(self.ffmpeg, self.fila).inspecionar(caminho)
        except Exception as erro:
            self.info = None
            self.var_info.set("Não foi possível ler o vídeo")
            self.log("Erro ao ler o vídeo: %s" % erro)
            return
        self.info = info
        self.var_info.set(info.resumo())
        if info.fps and info.fps < self.var_fps.get():
            valor = max(1, int(info.fps))
            self.var_fps.set(valor)
            self.escala_fps.set(valor)

    def escolher_saida(self):
        inicial = self.var_saida.get() or "saida.gif"
        caminho = filedialog.asksaveasfilename(
            title="Salvar GIF como",
            defaultextension=".gif",
            initialfile=os.path.basename(inicial),
            initialdir=os.path.dirname(inicial) or None,
            filetypes=[("GIF", "*.gif")],
        )
        if caminho:
            self.var_saida.set(caminho)

    def abrir_pasta(self):
        if not self.saida_final or not os.path.exists(self.saida_final):
            return
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(self.saida_final)])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(self.saida_final)])

    # ------------------------------------------------------------- conversão
    def _coletar_opcoes(self):
        entrada = self.var_entrada.get().strip()
        saida = self.var_saida.get().strip()
        if not entrada:
            raise ValueError("Escolha um vídeo de entrada.")
        if not os.path.isfile(entrada):
            raise ValueError("Arquivo de entrada não encontrado:\n%s" % entrada)
        if not saida:
            raise ValueError("Informe o arquivo GIF de saída.")
        if not saida.lower().endswith(".gif"):
            saida += ".gif"
            self.var_saida.set(saida)
        pasta = os.path.dirname(os.path.abspath(saida))
        if not os.path.isdir(pasta):
            raise ValueError("A pasta de saída não existe:\n%s" % pasta)
        if os.path.abspath(saida) == os.path.abspath(entrada):
            raise ValueError("O arquivo de saída não pode ser o mesmo da entrada.")

        try:
            fps = int(self.var_fps.get())
        except (tk.TclError, ValueError):
            raise ValueError("FPS inválido.")
        if not 1 <= fps <= 50:
            raise ValueError("O FPS deve estar entre 1 e 50.")

        larg_txt = self.var_largura.get().strip()
        if larg_txt.lower() in ("", "original"):
            largura = 0
        else:
            try:
                largura = int(larg_txt)
            except ValueError:
                raise ValueError("Largura inválida: %s" % larg_txt)
            if largura < 16:
                raise ValueError("A largura mínima é 16 px.")

        inicio = parse_tempo(self.var_inicio.get())
        duracao = parse_tempo(self.var_duracao.get())
        if duracao is not None and duracao <= 0:
            raise ValueError("A duração deve ser maior que zero.")
        if self.info and self.info.duracao and inicio and inicio >= self.info.duracao:
            raise ValueError("O início do recorte está depois do fim do vídeo.")

        if os.path.exists(saida):
            if not messagebox.askyesno(APP_NOME, "O arquivo já existe:\n%s\n\nSubstituir?" % saida):
                raise ValueError("")

        return {
            "entrada": entrada,
            "saida": saida,
            "fps": fps,
            "qualidade": self.var_qualidade.get(),
            "largura": largura,
            "inicio": inicio,
            "duracao": duracao,
            "loop": bool(self.var_loop.get()),
        }

    def iniciar(self):
        if self.thread and self.thread.is_alive():
            return
        try:
            opcoes = self._coletar_opcoes()
        except ValueError as erro:
            if str(erro):
                messagebox.showwarning(APP_NOME, str(erro))
            return

        self.texto.configure(state="normal")
        self.texto.delete("1.0", "end")
        self.texto.configure(state="disabled")
        self.log("ffmpeg: %s" % self.ffmpeg)
        self.barra["value"] = 0
        self.saida_final = None
        self.btn_converter.configure(state="disabled")
        self.btn_cancelar.configure(state="normal")
        self.btn_pasta.configure(state="disabled")
        self.var_status.set("Convertendo...")

        self.conversor = Conversor(self.ffmpeg, self.fila)
        self.thread = threading.Thread(target=self._trabalhar, args=(opcoes,), daemon=True)
        self.thread.start()

    def _trabalhar(self, opcoes):
        try:
            tamanho = self.conversor.converter(opcoes)
            self.fila.put(("ok", (opcoes["saida"], tamanho)))
        except Exception as erro:
            if self.conversor.cancelado:
                self.fila.put(("cancelado", None))
            else:
                self.fila.put(("erro", str(erro)))

    def cancelar(self):
        if self.conversor:
            self.var_status.set("Cancelando...")
            self.conversor.cancelar()

    def _finalizar(self):
        self.btn_converter.configure(state="normal")
        self.btn_cancelar.configure(state="disabled")

    def _drenar_fila(self):
        try:
            while True:
                tipo, dado = self.fila.get_nowait()
                if tipo == "log":
                    self.log(dado)
                elif tipo == "progresso":
                    self.barra["value"] = dado
                    self.var_status.set("Convertendo... %d%%" % dado)
                elif tipo == "ok":
                    caminho, tamanho = dado
                    self.saida_final = caminho
                    self.barra["value"] = 100
                    self.var_status.set("Concluído — %s (%s)"
                                        % (os.path.basename(caminho), formatar_tamanho(tamanho)))
                    self.log("")
                    self.log("Pronto! GIF salvo em: %s" % caminho)
                    self.log("Tamanho final: %s" % formatar_tamanho(tamanho))
                    self.btn_pasta.configure(state="normal")
                    self._finalizar()
                elif tipo == "cancelado":
                    self.barra["value"] = 0
                    self.var_status.set("Conversão cancelada.")
                    self.log("\nConversão cancelada pelo usuário.")
                    self._finalizar()
                elif tipo == "erro":
                    self.barra["value"] = 0
                    self.var_status.set("Erro na conversão.")
                    self.log("\nERRO: %s" % dado)
                    self._finalizar()
                    messagebox.showerror(APP_NOME, dado)
        except queue.Empty:
            pass
        self.after(100, self._drenar_fila)


def main():
    raiz = tk.Tk()
    raiz.title(APP_NOME)
    raiz.minsize(760, 640)
    estilo = ttk.Style(raiz)
    if "vista" in estilo.theme_names():
        estilo.theme_use("vista")
    elif "clam" in estilo.theme_names():
        estilo.theme_use("clam")

    ffmpeg = localizar_ffmpeg()
    if not ffmpeg:
        raiz.withdraw()
        messagebox.showerror(
            APP_NOME,
            "ffmpeg não encontrado.\n\n"
            "Instale o ffmpeg e adicione ao PATH, ou execute:\n"
            "    pip install imageio-ffmpeg",
        )
        raiz.destroy()
        return

    app = Aplicacao(raiz, ffmpeg)

    # aceita o vídeo como argumento: python video_para_gif.py filme.mp4
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        app.definir_entrada(sys.argv[1])

    raiz.mainloop()


if __name__ == "__main__":
    main()
