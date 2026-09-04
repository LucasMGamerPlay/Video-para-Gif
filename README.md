# Vídeo para GIF — contexto do projeto

Documento de referência do projeto: o que ele é, como funciona por dentro, o que
já foi construído e corrigido, e onde as coisas estão hoje.

Última atualização: 30/08/2026

---

## 1. O que é

Programa de desktop com interface gráfica que converte vídeo em GIF, com controle
de qualidade, FPS, resolução e recorte de trecho. Roda em Windows, escrito em
Python + Tkinter, usando ffmpeg como motor de conversão.

**Estado atual: funcionando e testado de ponta a ponta.** Todas as funcionalidades
descritas abaixo estão implementadas e verificadas com testes automatizados.

---

## 2. Repositório e arquivos

Repositório: <https://github.com/LucasMGamerPlay/Video-para-Gif>

O trabalho vai em branches, com merge para a `main` por pull request — foi assim
com o PR #1 (lançador `.pyw` e limpeza dos arquivos de teste).

| Arquivo | Papel |
|---|---|
| `video_para_gif.py` | O programa inteiro (~1330 linhas). Interface, timeline e motor de conversão. |
| `Video para GIF.pyw` | Lançador principal. Duplo clique abre a interface sem console. |
| `Video para GIF.bat` | Lançador alternativo, caso a associação `.pyw` do Windows falhe. |
| `CONTEXTO.md` | Este documento. |
| `.gitattributes` | Normalização de fim de linha (com regra específica para `.bat`, ver seção 7). |
| `.gitignore` | Ignora `__pycache__/` e `*.pyc`. |

Os dois lançadores aceitam um vídeo arrastado para cima do ícone — o caminho chega
como `sys.argv[1]` e já abre com o arquivo carregado.

---

## 3. Como rodar

```bash
pythonw "Video para GIF.pyw"
```

Ou duplo clique no `Video para GIF.pyw`. Para ver erros no console durante
desenvolvimento, use `python video_para_gif.py` (com `python`, não `pythonw`).

### Dependências

- **Python 3** com Tkinter (ambiente atual: Python 3.14.7, Tk 9.0)
- **ffmpeg** — procurado primeiro no `PATH`, com fallback para o binário embutido
  no pacote `imageio-ffmpeg`. No ambiente atual **não há ffmpeg no PATH**; o
  programa usa o ffmpeg 7.1 que vem no `imageio-ffmpeg` 0.6.0.
- **Pillow** — opcional. Sem ele o programa abre e converte normalmente, só sem as
  pré-visualizações e sem o play (o botão fica desabilitado).

Nada além disso precisa ser instalado.

> Detalhe do ambiente: há duas pastas de metadados do Pillow no mesmo
> `site-packages`. O `pip list` mostra 11.3.0, mas o que o Python realmente carrega
> é o **12.3.0**. É metadado velho que sobrou, não atrapalha — só não estranhe a
> divergência.

---

## 4. Interface

### Arquivos
Seleção do vídeo de entrada e do GIF de saída (sugerido automaticamente a partir do
nome do vídeo). Abaixo, um resumo do vídeo carregado: resolução, duração e fps.

### Qualidade e taxa de quadros

Presets de qualidade, que definem cores da paleta, dithering, modo de análise e uma
largura sugerida:

| Preset | Cores | Dithering | `stats_mode` | Largura sugerida |
|---|---|---|---|---|
| Baixa (arquivo menor) | 48 | `bayer:bayer_scale=5` | `diff` | 320 px |
| Média (equilibrada) | 128 | `bayer:bayer_scale=3` | `diff` | 480 px |
| Alta | 256 | `sierra2_4a` | `full` | 640 px |
| Máxima (arquivo maior) | 256 | `sierra2_4a` | `full` | Original |

Trocar o preset ajusta a largura, mas a largura é um controle independente
(`Original`, 240 a 1280 px) — dá para combinar qualidade alta com resolução baixa.
A altura sempre sai proporcional, com filtro `lanczos`.

**FPS** de 1 a 50, com slider e campo numérico sincronizados. Ao carregar um vídeo
com fps menor que o valor atual, o programa baixa o valor sozinho.

### Recorte

- **Dois painéis de pré-visualização** mostrando o quadro exato do início e do fim
  do corte, atualizados *durante* o arraste.
- **Timeline com filmstrip**: miniaturas do vídeo ao longo da barra, trecho
  selecionado destacado e o resto escurecido.
- **Dois marcadores arrastáveis** (laranja). Arrastar dentro da seleção move o
  trecho inteiro preservando a duração; clicar fora traz o marcador mais próximo.
  O cursor muda conforme o que está sob o mouse.
- **Campos de texto** `Início` e `Fim`, sincronizados nos dois sentidos com a
  timeline. Aceitam `mm:ss`, `hh:mm:ss` ou segundos.
- **Botão ▶ Reproduzir corte** — anima o trecho em loop, no FPS escolhido.
- **Botão Vídeo inteiro** — reseta a seleção.
- **Loop infinito** (ligado por padrão).

### Conversão
Barra de progresso real, botão Cancelar, registro do que o ffmpeg está fazendo, e
`Abrir pasta` ao terminar (mostra o tamanho final do arquivo).

---

## 5. Como funciona por dentro

### Conversão em duas passagens

O ponto central da qualidade. Em vez de converter direto para GIF, o programa:

1. **`palettegen`** — analisa o trecho e gera uma paleta otimizada de cores, salva
   num PNG temporário.
2. **`paletteuse`** — reconverte aplicando essa paleta com o dithering escolhido.

Isso dá um GIF muito melhor do que a conversão direta, que usaria uma paleta
genérica. A cadeia de filtros (`fps=N,scale=W:-1:flags=lanczos`) é idêntica nas duas
passagens — precisa ser, senão a paleta não corresponde aos quadros finais.

O recorte (`-ss` / `-t`) é aplicado **antes** do `-i` nas duas passagens. Quando a
seleção cobre o vídeo inteiro, o `-t` não é passado.

Loop: `-loop 0` para infinito, `-loop -1` para tocar uma vez.

### Modelo de threads

A interface nunca bloqueia. Tudo que chama ffmpeg roda fora da thread do Tk, e a
comunicação de volta é sempre por `queue.Queue` drenada com `after()`:

- **Conversão** — uma thread por conversão. Progresso lido do `-progress pipe:1`
  do ffmpeg (`out_time_us`), convertido em percentual (passagem 1 vale 35%,
  passagem 2 vale 65%). Cancelar chama `terminate()` no processo.
- **Pré-visualização dos marcadores** — uma thread permanente
  (`TrabalhadorPreview`) que guarda apenas o **pedido mais recente por painel**.
  Durante o arraste chegam dezenas de pedidos por segundo; processar a fila inteira
  faria a prévia ficar para trás do mouse. Descartando os pedidos velhos, ela
  acompanha.
- **Filmstrip** e **extração do clipe de play** — threads pontuais.

Regra importante: objetos `ImageTk.PhotoImage` só podem ser criados na thread do
Tk. As threads devolvem imagens PIL cruas; a conversão para `PhotoImage` acontece
sempre no `_drenar_fila`. As referências ficam guardadas em `self.fotos` e
`self.clipe`, senão o garbage collector apaga as imagens da tela.

### Timeline (`LinhaDoTempo`)

Um `tk.Canvas` que se redesenha inteiro a cada mudança. O filmstrip é regerado
quando a largura do widget muda (com 300ms de debounce, para não disparar a cada
pixel durante o redimensionamento da janela). As miniaturas passam por `cobrir()`,
que redimensiona cobrindo a área e corta o excesso, em vez de distorcer.

Os marcadores não podem se cruzar: há uma seleção mínima de 0,1s.

### Reprodução do corte

Ao dar play, o ffmpeg extrai os quadros do trecho como PNGs numa pasta temporária,
no FPS escolhido e na largura do painel. Os dois painéis de início/fim são
escondidos (`grid_remove`) e um painel único ocupa o lugar, animando em loop.

O clipe fica em cache identificado por uma **assinatura**
`(arquivo, início, fim, fps, largura do painel)`. Dar play de novo no mesmo trecho é
instantâneo. Qualquer coisa que mude a assinatura — arrastar um marcador, digitar
outro tempo, mexer no FPS — para a reprodução e descarta o cache, para nunca exibir
uma prévia desatualizada. Iniciar uma conversão também para o play.

Cortes longos têm o FPS da prévia reduzido para respeitar um teto de 300 quadros
(`LIMITE_QUADROS_PREVIA`), evitando estourar a memória. Isso afeta só a prévia,
nunca o GIF final.

---

## 6. Decisões de design

**Por que ffmpeg em vez de moviepy/imageio.** Duas passagens com paleta é o que
separa um GIF bom de um GIF ruim, e o ffmpeg faz isso nativamente. O `imageio-ffmpeg`
entra só como fornecedor do binário.

**Por que `ffmpeg -i` e regex em vez de ffprobe.** O `imageio-ffmpeg` empacota
apenas o `ffmpeg`, sem o `ffprobe`. Duração, resolução e fps saem do texto que o
`ffmpeg -i` escreve no stderr.

**Por que o campo virou `Fim` e não `Duração`.** Com dois marcadores arrastáveis, o
que o usuário manipula é o ponto final — "fim" é o que casa com o gesto. A duração
virou um texto calculado ao lado. Internamente ainda se converte para `-t` na hora
de chamar o ffmpeg.

**Por que `.pyw` é o lançador principal.** A extensão `.pyw` já é associada ao
`pyw.exe` no Windows, então o duplo clique abre a interface sem console, sem passar
por batch nem pelo comando `start`. É o caminho com menos peças para quebrar.

**Por que a prévia de play não mostra a qualidade final.** Ela usa `scale` bilinear
direto, sem `palettegen`/`paletteuse`. Fazer a prévia com a paleta real custaria o
mesmo tempo de uma conversão completa. A prévia serve para conferir **tempo e
movimento**; as cores só aparecem no resultado final.

---

## 7. Problemas encontrados e corrigidos

### GIF parcial ficava no disco ao cancelar
Cancelar no meio da segunda passagem deixava um GIF incompleto no destino —
inclusive por cima de um arquivo bom que já existisse ali. Corrigido: o `converter()`
apaga o arquivo de saída em qualquer exceção, cancelamento incluído.

### `.bat` com quebras de linha LF (erro `Windows não pode localizar '\\'`)
O lançador `.bat` foi criado com quebras de linha LF. O `cmd.exe` exige CRLF: com LF
ele lê o arquivo com offsets errados e acaba executando fragmentos de linha, o que
produziu um diálogo do Windows tentando abrir `\\`. Corrigido reescrevendo o arquivo
com CRLF, e adicionando o lançador `.pyw`, que não depende de batch nem do `start`.

### `.gitattributes` reintroduziria o bug do `.bat`
O `.gitattributes` tinha só `* text=auto`, que normaliza tudo para LF no
repositório. Num próximo checkout o `.bat` poderia voltar com LF e quebrar de novo.
Corrigido fixando `*.bat text eol=crlf`.

### Prévia tocava 12 quadros/s em vez de 15
A reprodução encadeava `after(67ms)` a cada quadro. Como o `after` só começa a contar
**depois** que o callback termina, o tempo de renderização se somava a cada quadro e
a prévia ia ficando ~20% mais lenta que o GIF real. Corrigido derivando o índice do
quadro do relógio (`time.monotonic()`): se atrasar, descarta quadros em vez de
arrastar o ritmo. Desvio medido depois da correção: −0,1%.

### Arquivos de teste versionados
Um arquivo vazio chamado `pythonw` (criado por um redirecionamento acidental durante
os testes) e o `__pycache__` tinham entrado no commit inicial. Removidos do
versionamento, com `.gitignore` para não voltarem.

---

## 8. O que foi testado

Testes automatizados rodados contra vídeos gerados na hora com `testsrc` do ffmpeg
(um 640x360 30fps 4s e um vertical 360x640 25fps 6s).

**Conversão**
- Os 4 presets de qualidade, conferindo tamanho e resolução de saída
- FPS aplicado de verdade: 12fps → 48 quadros em 4s; 8fps → 16 quadros em 2s
- Recorte 1s→3s produzindo um GIF de exatamente 2,00s
- Flag de loop: infinito grava `loop=0`; desligado não grava loop
- Cancelamento no meio, sem deixar arquivo parcial
- Validações: tempo inválido, fim menor que o início, saída igual à entrada,
  arquivo já existente, pasta inexistente, FPS fora da faixa

**Timeline**
- Arraste dos três modos (início, fim, região) com os campos acompanhando
- Limites: arrastar para fora trava em 0 e na duração; marcadores não se cruzam
- Digitar tempo válido move os marcadores; texto inválido é ignorado sem quebrar
- Filmstrip gerado e ambos os previews carregados
- Vídeo vertical 9:16 → painéis viram 110x195, mantendo a proporção
- 60 eventos de arraste despachados em 9ms, sem travar a interface

**Reprodução**
- Play, parar, troca dos painéis, loop dando a volta sozinho
- Cache reaproveitado (replay em 5ms)
- Invalidação por arraste e por mudança de FPS
- Play parando ao iniciar uma conversão
- Ritmo: 10 / 15 / 25 fps medidos contra o relógio, desvio de −0,1% ou menos

**Interface**
- Janela abrindo por duplo clique e por arrastar-e-soltar um vídeo
- Tamanho natural da janela (640x881) cabe na tela de 1920x1080

---

## 9. Limitações conhecidas

- **A prévia de play não mostra a qualidade final** — só tempo e movimento. As cores
  da paleta escolhida aparecem apenas no GIF gerado.
- **Cortes muito longos truncam a prévia.** O teto de 300 quadros é garantido pelo
  `-frames:v`, mas num corte acima de ~5 minutos o FPS reduzido não chega a cobrir o
  trecho inteiro — a prévia mostra só o começo. A conversão final não é afetada.
- **O `.bat` não foi testado de ponta a ponta.** O comando `start` estava bloqueado
  no ambiente onde os testes rodaram (negava até `cmd /c exit`). A estrutura do batch
  foi validada com trace, mas quem foi verificado abrindo de verdade é o `.pyw`.
- **Sem áudio**, por definição — GIF não tem trilha sonora.
- **Leitura de metadados por regex** sobre a saída do `ffmpeg -i`. Funciona nos
  formatos testados, mas é mais frágil que ffprobe com saída JSON.
- **Sem barra de progresso na extração do clipe de play.** Em vídeos grandes há uma
  espera de alguns segundos mostrando só "montando a prévia...".

---

## 10. Ideias para depois

- Prévia com a paleta real (custa uma conversão completa — talvez só sob demanda)
- Limite de tamanho de arquivo, ajustando resolução/fps automaticamente para caber
- Conversão em lote de vários vídeos
- Lembrar as últimas preferências entre sessões
- Corte por arraste direto no filmstrip com zoom, para vídeos longos
- Empacotar como `.exe` com PyInstaller, para rodar sem Python instalado
