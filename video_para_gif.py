# -*- coding: utf-8 -*-
"""
Vídeo para GIF
Conversor de vídeo para GIF com interface gráfica (Tkinter).

Usa ffmpeg em duas passagens (palettegen + paletteuse), o que produz
GIFs com qualidade muito superior à conversão direta.

Requisitos: ffmpeg no PATH ou o pacote Python "imageio-ffmpeg".
"""

import io
import math
import os
import re
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from PIL import Image, ImageTk

    PREVIEW_DISPONIVEL = True
except ImportError:  # sem Pillow o programa funciona, só sem pré-visualização
    PREVIEW_DISPONIVEL = False

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


def formatar_tempo_campo(segundos):
    """Tempo com um decimal, do jeito que vai para os campos de recorte."""
    segundos = max(0.0, segundos)
    horas = int(segundos // 3600)
    minutos = int((segundos % 3600) // 60)
    resto = segundos % 60
    if horas:
        return "%d:%02d:%04.1f" % (horas, minutos, resto)
    return "%d:%04.1f" % (minutos, resto)


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


def extrair_frame(ffmpeg, caminho, tempo, largura):
    """Extrai um único quadro do vídeo e devolve uma imagem PIL (ou None)."""
    if not PREVIEW_DISPONIVEL:
        return None
    proc = subprocess.run(
        [
            ffmpeg, "-hide_banner", "-nostdin",
            "-ss", "%.3f" % max(0.0, tempo),
            "-i", caminho,
            "-frames:v", "1",
            "-vf", "scale=%d:-1:flags=bilinear" % largura,
            "-f", "image2pipe", "-vcodec", "png", "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        startupinfo=_STARTUPINFO,
        creationflags=_CREATE_NO_WINDOW,
    )
    if not proc.stdout:
        return None
    try:
        return Image.open(io.BytesIO(proc.stdout)).convert("RGB")
    except Exception:
        return None


def cobrir(imagem, largura, altura):
    """Redimensiona cobrindo a área e corta o excesso (sem distorcer)."""
    escala = max(largura / imagem.width, altura / imagem.height)
    nova = imagem.resize(
        (max(1, int(imagem.width * escala + 0.5)), max(1, int(imagem.height * escala + 0.5))),
        Image.BILINEAR,
    )
    esq = (nova.width - largura) // 2
    topo = (nova.height - altura) // 2
    return nova.crop((esq, topo, esq + largura, topo + altura))


LIMITE_QUADROS_PREVIA = 300


def fps_da_previa(fps, duracao):
    """Reduz o fps da prévia em cortes longos, para não estourar a memória."""
    if duracao <= 0:
        return float(fps)
    return min(float(fps), max(1.0, LIMITE_QUADROS_PREVIA / duracao))


def extrair_clipe(ffmpeg, caminho, inicio, duracao, fps, largura):
    """Extrai os quadros do trecho selecionado, para reproduzir a prévia."""
    if not PREVIEW_DISPONIVEL:
        return []
    pasta = tempfile.mkdtemp(prefix="video2gif_previa_")
    try:
        args = [ffmpeg, "-hide_banner", "-nostdin", "-y"]
        if inicio:
            args += ["-ss", "%.3f" % inicio]
        if duracao:
            args += ["-t", "%.3f" % duracao]
        args += [
            "-i", caminho,
            "-vf", "fps=%.4f,scale=%d:-1:flags=bilinear" % (fps, largura),
            "-frames:v", str(LIMITE_QUADROS_PREVIA),
            os.path.join(pasta, "q%05d.png"),
        ]
        subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       startupinfo=_STARTUPINFO, creationflags=_CREATE_NO_WINDOW)
        imagens = []
        for nome in sorted(os.listdir(pasta)):
            try:
                with Image.open(os.path.join(pasta, nome)) as im:
                    imagens.append(im.convert("RGB"))
            except Exception:
                pass
        return imagens
    finally:
        shutil.rmtree(pasta, ignore_errors=True)


class TrabalhadorPreview(threading.Thread):
    """Extrai quadros em segundo plano, sempre atendendo o pedido mais recente.

    Durante o arraste chegam dezenas de pedidos por segundo; guardar só o
    último por painel mantém a pré-visualização acompanhando o mouse em vez
    de ficar processando uma fila atrasada.
    """

    def __init__(self, ffmpeg, entregar):
        threading.Thread.__init__(self, daemon=True)
        self.ffmpeg = ffmpeg
        self.entregar = entregar
        self.pendentes = {}
        self.cond = threading.Condition()
        self.encerrar = False

    def pedir(self, chave, caminho, tempo, largura):
        with self.cond:
            self.pendentes[chave] = (caminho, tempo, largura)
            self.cond.notify()

    def parar(self):
        with self.cond:
            self.encerrar = True
            self.cond.notify()

    def run(self):
        while True:
            with self.cond:
                while not self.pendentes and not self.encerrar:
                    self.cond.wait()
                if self.encerrar:
                    return
                chave = next(iter(self.pendentes))
                caminho, tempo, largura = self.pendentes.pop(chave)
            imagem = extrair_frame(self.ffmpeg, caminho, tempo, largura)
            if imagem is not None:
                self.entregar(chave, tempo, imagem)


class LinhaDoTempo(tk.Canvas):
    """Barra de recorte com filmstrip e dois marcadores arrastáveis."""

    ALTURA = 74
    MARGEM = 12
    TOPO_STRIP = 7
    ALTURA_STRIP = 60
    TOLERANCIA = 9          # px de folga para pegar um marcador
    SELECAO_MINIMA = 0.10   # segundos

    COR_FUNDO = "#242424"
    COR_VAZIO = "#3a3a3a"
    COR_ALCA = "#f5a623"
    COR_BORDA = "#f5a623"

    def __init__(self, master, ffmpeg, ao_mudar):
        tk.Canvas.__init__(self, master, height=self.ALTURA, highlightthickness=0,
                           background=self.COR_FUNDO, cursor="arrow")
        self.ffmpeg = ffmpeg
        self.ao_mudar = ao_mudar
        self.caminho = None
        self.duracao = 0.0
        self.aspecto = 16.0 / 9.0
        self.inicio = 0.0
        self.fim = 0.0
        self.miniaturas = []
        self.fila = queue.Queue()
        self._arrastando = None
        self._deslocamento = 0.0
        self._job_strip = None
        self._largura_strip = 0
        self.bind("<Configure>", self._ao_redimensionar)
        self.bind("<ButtonPress-1>", self._pressionar)
        self.bind("<B1-Motion>", self._mover)
        self.bind("<ButtonRelease-1>", self._soltar)
        self.bind("<Motion>", self._atualizar_cursor)
        self.after(80, self._drenar)

    # ------------------------------------------------------------- geometria
    def _util(self):
        return max(1, self.winfo_width() - 2 * self.MARGEM)

    def _t_para_x(self, t):
        if not self.duracao:
            return self.MARGEM
        return self.MARGEM + self._util() * (t / self.duracao)

    def _x_para_t(self, x):
        if not self.duracao:
            return 0.0
        return min(self.duracao, max(0.0, (x - self.MARGEM) / self._util() * self.duracao))

    # ----------------------------------------------------------------- estado
    def carregar(self, caminho, duracao, aspecto):
        self.caminho = caminho
        self.duracao = duracao or 0.0
        self.aspecto = aspecto or (16.0 / 9.0)
        self.inicio = 0.0
        self.fim = self.duracao
        self.miniaturas = []
        self._largura_strip = 0
        self._desenhar()
        self._agendar_filmstrip()

    def limpar(self):
        self.caminho = None
        self.duracao = 0.0
        self.inicio = self.fim = 0.0
        self.miniaturas = []
        self._largura_strip = 0
        self._desenhar()

    def definir_selecao(self, inicio, fim):
        """Usado quando o usuário digita os tempos nos campos de texto."""
        if not self.duracao:
            return
        inicio = min(max(0.0, inicio), self.duracao)
        fim = min(max(inicio + self.SELECAO_MINIMA, fim), self.duracao)
        self.inicio, self.fim = inicio, fim
        self._desenhar()

    # ---------------------------------------------------------------- desenho
    def _desenhar(self):
        self.delete("all")
        largura, altura = self.winfo_width(), self.winfo_height()
        if largura <= 1:
            return
        y0, y1 = self.TOPO_STRIP, self.TOPO_STRIP + self.ALTURA_STRIP

        if not self.duracao:
            self.create_rectangle(self.MARGEM, y0, largura - self.MARGEM, y1,
                                  fill=self.COR_VAZIO, outline="")
            self.create_text(largura / 2, (y0 + y1) / 2,
                             text="Carregue um vídeo para recortar",
                             fill="#8a8a8a", font=("Segoe UI", 9))
            return

        if self.miniaturas:
            x = self.MARGEM
            for img in self.miniaturas:
                self.create_image(x, y0, image=img, anchor="nw")
                x += img.width()
        else:
            self.create_rectangle(self.MARGEM, y0, largura - self.MARGEM, y1,
                                  fill=self.COR_VAZIO, outline="")

        # esconde o que passar das margens
        self.create_rectangle(0, 0, self.MARGEM, altura, fill=self.COR_FUNDO, outline="")
        self.create_rectangle(largura - self.MARGEM, 0, largura, altura,
                              fill=self.COR_FUNDO, outline="")

        xi, xf = self._t_para_x(self.inicio), self._t_para_x(self.fim)

        # escurece o que ficou fora da seleção
        for a, b in ((self.MARGEM, xi), (xf, largura - self.MARGEM)):
            if b > a:
                self.create_rectangle(a, y0, b, y1, fill="#000000",
                                      stipple="gray50", outline="")

        self.create_rectangle(xi, y0, xf, y1, outline=self.COR_BORDA, width=2)

        for x in (xi, xf):
            self.create_rectangle(x - 4, 0, x + 4, altura,
                                  fill=self.COR_ALCA, outline="#7a4d00")
            for dy in (-5, 0, 5):
                self.create_line(x - 1.5, altura / 2 + dy, x + 1.5, altura / 2 + dy,
                                 fill="#5a3900")

    def _ao_redimensionar(self, _ev=None):
        self._desenhar()
        self._agendar_filmstrip()

    # -------------------------------------------------------------- filmstrip
    def _agendar_filmstrip(self):
        if self._job_strip:
            try:
                self.after_cancel(self._job_strip)
            except Exception:
                pass
        self._job_strip = self.after(300, self._gerar_filmstrip)

    def _gerar_filmstrip(self):
        self._job_strip = None
        if not (self.caminho and self.duracao and PREVIEW_DISPONIVEL):
            return
        largura = self.winfo_width()
        if largura <= 1 or largura == self._largura_strip:
            return
        self._largura_strip = largura

        util = largura - 2 * self.MARGEM
        larg_mini = max(40, int(self.ALTURA_STRIP * self.aspecto))
        quantidade = max(1, int(math.ceil(util / float(larg_mini))))
        caminho, duracao = self.caminho, self.duracao

        def trabalho():
            imagens = []
            for i in range(quantidade):
                t = duracao * (i + 0.5) / quantidade
                imagens.append(extrair_frame(self.ffmpeg, caminho,
                                             min(t, max(0.0, duracao - 0.05)),
                                             larg_mini))
            self.fila.put((caminho, larg_mini, imagens))

        threading.Thread(target=trabalho, daemon=True).start()

    def _drenar(self):
        try:
            while True:
                caminho, larg_mini, imagens = self.fila.get_nowait()
                if caminho != self.caminho:
                    continue  # o usuário já trocou de vídeo
                fotos = []
                for img in imagens:
                    if img is None:
                        continue
                    fotos.append(ImageTk.PhotoImage(
                        cobrir(img, larg_mini, self.ALTURA_STRIP)))
                if fotos:
                    self.miniaturas = fotos
                    self._desenhar()
        except queue.Empty:
            pass
        self.after(80, self._drenar)

    # ---------------------------------------------------------------- eventos
    def _alca_em(self, x):
        xi, xf = self._t_para_x(self.inicio), self._t_para_x(self.fim)
        if abs(x - xi) <= self.TOLERANCIA:
            return "inicio"
        if abs(x - xf) <= self.TOLERANCIA:
            return "fim"
        if xi < x < xf:
            return "regiao"
        return None

    def _atualizar_cursor(self, ev):
        if not self.duracao:
            return
        alvo = self._alca_em(ev.x)
        if alvo in ("inicio", "fim"):
            self.configure(cursor="sb_h_double_arrow")
        elif alvo == "regiao":
            self.configure(cursor="fleur")
        else:
            self.configure(cursor="arrow")

    def _pressionar(self, ev):
        if not self.duracao:
            return
        alvo = self._alca_em(ev.x)
        if alvo is None:
            # clicou fora da seleção: traz o marcador mais próximo até ali
            xi, xf = self._t_para_x(self.inicio), self._t_para_x(self.fim)
            alvo = "inicio" if abs(ev.x - xi) <= abs(ev.x - xf) else "fim"
        self._arrastando = alvo
        if alvo == "regiao":
            self._deslocamento = self._x_para_t(ev.x) - self.inicio
        else:
            self._aplicar(ev.x)
        self._notificar(True)

    def _mover(self, ev):
        if not self._arrastando:
            return
        self._aplicar(ev.x)
        self._notificar(True)

    def _soltar(self, _ev):
        if not self._arrastando:
            return
        self._arrastando = None
        self._notificar(False)

    def _aplicar(self, x):
        t = self._x_para_t(x)
        if self._arrastando == "inicio":
            self.inicio = min(t, self.fim - self.SELECAO_MINIMA)
            self.inicio = max(0.0, self.inicio)
        elif self._arrastando == "fim":
            self.fim = max(t, self.inicio + self.SELECAO_MINIMA)
            self.fim = min(self.duracao, self.fim)
        elif self._arrastando == "regiao":
            largura_sel = self.fim - self.inicio
            novo = min(max(0.0, t - self._deslocamento), self.duracao - largura_sel)
            self.inicio, self.fim = novo, novo + largura_sel
        self._desenhar()

    def _notificar(self, arrastando):
        if self.ao_mudar:
            self.ao_mudar(self._arrastando, self.inicio, self.fim, arrastando)


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
        self.var_fim = tk.StringVar()
        self.var_loop = tk.BooleanVar(value=True)
        self.var_status = tk.StringVar(value="Pronto. Selecione um vídeo.")
        self.var_info = tk.StringVar(value="Nenhum vídeo selecionado")
        self.var_resumo = tk.StringVar(value="")
        self.var_cap = {"inicio": tk.StringVar(value="Início"),
                        "fim": tk.StringVar(value="Fim"),
                        "play": tk.StringVar(value="Prévia do corte")}

        # pré-visualização do corte
        self.paineis = {}
        self.caixas = {}
        self.fotos = {}

        # reprodução do trecho selecionado
        self.clipe = []
        self.clipe_assinatura = None
        self.clipe_fps = 15.0
        self.tocando = False
        self.job_play = None
        self.indice_quadro = 0
        self.inicio_play = 0.0
        self.larg_preview = 240
        self.alt_preview = 135
        self.trabalhador = None
        if PREVIEW_DISPONIVEL:
            self.trabalhador = TrabalhadorPreview(
                ffmpeg,
                lambda chave, tempo, img: self.fila.put(("preview", (chave, tempo, img))),
            )
            self.trabalhador.start()

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
        rc = ttk.LabelFrame(self, text=" Recorte ", padding=10)
        rc.grid(row=linha, column=0, sticky="ew", pady=(10, 0))
        rc.columnconfigure(0, weight=1)

        pv = ttk.Frame(rc)
        pv.grid(row=0, column=0)
        self._painel_preview(pv, 0, "inicio")
        self._painel_preview(pv, 1, "fim")
        # ocupa o lugar dos dois durante a reprodução
        self._painel_preview(pv, 0, "play", colunas=2)
        self.caixas["play"].grid_remove()

        self.timeline = LinhaDoTempo(rc, self.ffmpeg, self._timeline_mudou)
        self.timeline.grid(row=1, column=0, sticky="ew", pady=(10, 6))

        ct = ttk.Frame(rc)
        ct.grid(row=2, column=0, sticky="ew")
        ct.columnconfigure(5, weight=1)

        ttk.Label(ct, text="Início:").grid(row=0, column=0, sticky="w")
        campo_ini = ttk.Entry(ct, textvariable=self.var_inicio, width=11)
        campo_ini.grid(row=0, column=1, padx=(6, 18))
        ttk.Label(ct, text="Fim:").grid(row=0, column=2, sticky="w")
        campo_fim = ttk.Entry(ct, textvariable=self.var_fim, width=11)
        campo_fim.grid(row=0, column=3, padx=(6, 18))
        for campo in (campo_ini, campo_fim):
            campo.bind("<Return>", self._campos_mudaram)
            campo.bind("<FocusOut>", self._campos_mudaram)

        ttk.Label(ct, textvariable=self.var_resumo, foreground="#2f6fae").grid(
            row=0, column=4, sticky="w")
        self.btn_play = ttk.Button(ct, text="▶  Reproduzir corte",
                                   command=self.alternar_play)
        self.btn_play.grid(row=0, column=6, sticky="e")
        if not PREVIEW_DISPONIVEL:
            self.btn_play.configure(state="disabled")
        ttk.Button(ct, text="Vídeo inteiro", command=self.selecionar_tudo).grid(
            row=0, column=7, sticky="e", padx=(8, 0))

        ttk.Label(ct, text="arraste os marcadores laranja, ou digite  mm:ss  /  hh:mm:ss",
                  foreground="#777").grid(row=1, column=0, columnspan=5, sticky="w", pady=(8, 0))
        ttk.Checkbutton(ct, text="Repetir em loop infinito", variable=self.var_loop).grid(
            row=2, column=0, columnspan=5, sticky="w", pady=(6, 0))

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
        self._invalidar_clipe()

    def _spin_mudou(self):
        try:
            self.escala_fps.set(self.var_fps.get())
        except tk.TclError:
            pass
        self._invalidar_clipe()

    def _qualidade_mudou(self, _evento=None):
        preset = QUALIDADES[self.var_qualidade.get()]
        self.var_largura.set("Original" if preset["largura"] == 0 else str(preset["largura"]))

    # ------------------------------------------------------- recorte / preview
    def _painel_preview(self, pai, coluna, chave, colunas=1):
        caixa = ttk.Frame(pai)
        caixa.grid(row=0, column=coluna, columnspan=colunas, padx=8)
        self.caixas[chave] = caixa
        ttk.Label(caixa, textvariable=self.var_cap[chave]).grid(row=0, column=0, pady=(0, 4))
        moldura = tk.Frame(caixa, width=self.larg_preview, height=self.alt_preview,
                           background="#1a1a1a", relief="solid", borderwidth=1)
        moldura.grid(row=1, column=0)
        moldura.grid_propagate(False)
        rotulo = tk.Label(moldura, background="#1a1a1a", foreground="#666",
                          text="sem vídeo" if PREVIEW_DISPONIVEL
                          else "instale o Pillow\npara ver a prévia")
        rotulo.place(relx=0.5, rely=0.5, anchor="center")
        self.paineis[chave] = (moldura, rotulo)

    def _ajustar_paineis(self, aspecto):
        """Mantém os dois painéis com a proporção do vídeo, sem estourar a janela."""
        aspecto = aspecto or (16.0 / 9.0)
        self.larg_preview = max(110, min(260, int(146 * aspecto)))
        self.alt_preview = max(80, int(self.larg_preview / aspecto))
        for moldura, rotulo in self.paineis.values():
            moldura.configure(width=self.larg_preview, height=self.alt_preview)
            rotulo.configure(image="", text="carregando...")
        self.fotos.clear()

    def _pedir_preview(self, chave, tempo):
        if not (self.trabalhador and self.timeline.caminho and self.timeline.duracao):
            return
        limite = max(0.0, self.timeline.duracao - 0.05)
        self.trabalhador.pedir(chave, self.timeline.caminho,
                               min(max(0.0, tempo), limite), self.larg_preview)

    def _timeline_mudou(self, alvo, inicio, fim, _arrastando):
        self._invalidar_clipe()
        self.var_inicio.set(formatar_tempo_campo(inicio))
        self.var_fim.set(formatar_tempo_campo(fim))
        self.var_cap["inicio"].set("Início   %s" % formatar_tempo_campo(inicio))
        self.var_cap["fim"].set("Fim   %s" % formatar_tempo_campo(fim))
        self.var_resumo.set("corte de %s" % formatar_tempo_campo(max(0.0, fim - inicio)))
        if alvo in (None, "inicio", "regiao"):
            self._pedir_preview("inicio", inicio)
        if alvo in (None, "fim", "regiao"):
            # o último quadro do GIF é o de logo antes do marcador de fim
            self._pedir_preview("fim", fim - 0.04)

    def _campos_mudaram(self, _evento=None):
        if not self.timeline.duracao:
            return
        try:
            inicio = parse_tempo(self.var_inicio.get())
            fim = parse_tempo(self.var_fim.get())
        except ValueError:
            return
        self.timeline.definir_selecao(
            0.0 if inicio is None else inicio,
            self.timeline.duracao if fim is None else fim,
        )
        self._timeline_mudou(None, self.timeline.inicio, self.timeline.fim, False)

    def selecionar_tudo(self):
        if not self.timeline.duracao:
            return
        self.timeline.definir_selecao(0.0, self.timeline.duracao)
        self._timeline_mudou(None, self.timeline.inicio, self.timeline.fim, False)

    # ------------------------------------------------- reprodução do trecho
    def _assinatura_clipe(self):
        """Identifica o trecho já extraído, para reaproveitar ao dar play de novo."""
        try:
            fps = int(self.var_fps.get())
        except (tk.TclError, ValueError):
            fps = 15
        return (self.timeline.caminho, round(self.timeline.inicio, 2),
                round(self.timeline.fim, 2), fps, self.larg_preview)

    def alternar_play(self):
        if self.tocando:
            self._parar_play()
        else:
            self._preparar_clipe()

    def _preparar_clipe(self):
        if not (PREVIEW_DISPONIVEL and self.timeline.caminho and self.timeline.duracao):
            return
        assinatura = self._assinatura_clipe()
        if self.clipe and self.clipe_assinatura == assinatura:
            self._iniciar_reproducao()
            return

        inicio, fim = self.timeline.inicio, self.timeline.fim
        duracao = max(0.05, fim - inicio)
        fps = fps_da_previa(assinatura[3], duracao)
        caminho, largura = self.timeline.caminho, self.larg_preview

        self.btn_play.configure(state="disabled", text="preparando...")
        self.var_cap["play"].set("montando a prévia...")

        def trabalho():
            imagens = extrair_clipe(self.ffmpeg, caminho, inicio, duracao, fps, largura)
            self.fila.put(("clipe", (assinatura, imagens, fps)))

        threading.Thread(target=trabalho, daemon=True).start()

    def _iniciar_reproducao(self):
        if not self.clipe:
            return
        self.caixas["inicio"].grid_remove()
        self.caixas["fim"].grid_remove()
        self.caixas["play"].grid()
        self.tocando = True
        self.indice_quadro = 0
        self.inicio_play = time.monotonic()
        self.btn_play.configure(state="normal", text="■  Parar")
        self._proximo_quadro()

    def _proximo_quadro(self):
        # o quadro sai do relógio, não de um contador: encadear after() vai
        # acumulando o tempo de render e a prévia acaba tocando mais devagar
        # que o GIF de verdade.
        if not (self.tocando and self.clipe):
            return
        total = len(self.clipe)
        numero = int((time.monotonic() - self.inicio_play) * self.clipe_fps)
        indice = numero % total
        self.paineis["play"][1].configure(image=self.clipe[indice], text="")
        self.var_cap["play"].set("reproduzindo   %s   (%d/%d)" % (
            formatar_tempo_campo(self.timeline.inicio + indice / self.clipe_fps),
            indice + 1, total))
        self.indice_quadro = indice
        alvo = self.inicio_play + (numero + 1) / self.clipe_fps
        atraso = int(round((alvo - time.monotonic()) * 1000))
        self.job_play = self.after(max(5, atraso), self._proximo_quadro)

    def _parar_play(self):
        self.tocando = False
        if self.job_play:
            try:
                self.after_cancel(self.job_play)
            except Exception:
                pass
            self.job_play = None
        self.caixas["play"].grid_remove()
        self.caixas["inicio"].grid()
        self.caixas["fim"].grid()
        self.btn_play.configure(state="normal", text="▶  Reproduzir corte")

    def _invalidar_clipe(self):
        """O trecho mudou: para a reprodução e joga fora os quadros extraídos."""
        if self.tocando:
            self._parar_play()
        self.clipe = []
        self.clipe_assinatura = None

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
            self.timeline.limpar()
            self.var_info.set("Não foi possível ler o vídeo")
            self.log("Erro ao ler o vídeo: %s" % erro)
            return
        self.info = info
        self.var_info.set(info.resumo())
        if info.fps and info.fps < self.var_fps.get():
            valor = max(1, int(info.fps))
            self.var_fps.set(valor)
            self.escala_fps.set(valor)

        if info.duracao:
            aspecto = (float(info.largura) / info.altura) if (info.largura and info.altura) else None
            self._ajustar_paineis(aspecto)
            self.timeline.carregar(caminho, info.duracao, aspecto)
            self._timeline_mudou(None, 0.0, info.duracao, False)
        else:
            self.timeline.limpar()
            self.var_resumo.set("")

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
        fim = parse_tempo(self.var_fim.get())
        if self.info and self.info.duracao and inicio and inicio >= self.info.duracao:
            raise ValueError("O início do recorte está depois do fim do vídeo.")

        duracao = None
        if fim is not None:
            if fim <= (inicio or 0.0):
                raise ValueError("O fim do recorte precisa ser maior que o início.")
            # cobrindo o vídeo inteiro não vale a pena passar -t ao ffmpeg
            fim_do_video = self.info.duracao if (self.info and self.info.duracao) else None
            if fim_do_video is None or fim < fim_do_video - 0.01:
                duracao = fim - (inicio or 0.0)

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

        self._parar_play()
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
                elif tipo == "preview":
                    chave, _tempo, imagem = dado
                    _moldura, rotulo = self.paineis[chave]
                    foto = ImageTk.PhotoImage(
                        cobrir(imagem, self.larg_preview, self.alt_preview))
                    self.fotos[chave] = foto  # segura a referência, senão some
                    rotulo.configure(image=foto, text="")
                elif tipo == "clipe":
                    assinatura, imagens, fps = dado
                    self.btn_play.configure(state="normal",
                                            text="▶  Reproduzir corte")
                    if assinatura != self._assinatura_clipe():
                        continue  # o corte mudou enquanto a previa era montada
                    if not imagens:
                        self.var_cap["play"].set("nao foi possivel montar a previa")
                        continue
                    self.clipe = [
                        ImageTk.PhotoImage(cobrir(im, self.larg_preview, self.alt_preview))
                        for im in imagens
                    ]
                    self.clipe_assinatura = assinatura
                    self.clipe_fps = fps
                    self._iniciar_reproducao()
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
    raiz.minsize(780, 700)
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
