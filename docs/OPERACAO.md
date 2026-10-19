# Operação no Jetson Xavier NX

## Percurso do vídeo

1. A SIYI A8 Mini apresenta o vídeo em `rtsp://192.168.144.25:8554/main.264`. Apesar do sufixo `.264`, a configuração dos scripts usa H.265.
2. O Jetson usa GStreamer com `nvv4l2decoder`, converte o frame para BGR e entrega-o ao OpenCV.
3. A thread de inferência usa YOLO11s ou SAHI, **alternativamente**, para detetar apenas a classe `person` (`0`).
4. As deteções recentes são desenhadas sobre o vídeo.
5. Um servidor RTSP em `8555/camera` recodifica os frames em H.264 com `nvv4l2h264enc`.
6. O ATLAS Video Control pode abrir a stream RTSP processada através da rede HM30.

## Parâmetros lidos do código

| Variável | Valor |
| --- | --- |
| IP da A8 | `192.168.144.25` |
| Entrada RTSP | `8554/main.264` |
| Codec de entrada | H.265 |
| Latência de receção configurada | 150 ms (buffer GStreamer, não latência total medida) |
| Modelo | `yolo11s.pt` |
| Deteção | Classe 0: pessoa |
| Confiança | 0.25 |
| YOLO `imgsz` | 640 |
| SAHI slice | 640 × 640 |
| SAHI overlap | 0.15 |
| SAHI inicial | 1 inferência a cada 2 oportunidades de processamento |
| Porta RTSP de saída | 8555 |
| Mount RTSP | `/camera` |
| Codec de saída | H.264 |
| Bitrate de saída configurado | 1.5 Mbps |
| FPS de saída configurado | 25 |
| I-frame interval | 50 |

No modo SAHI, as oportunidades ignoradas **não executam YOLO**. A thread de inferência não promete produzir resultados a cada frame de saída.

## Modo com monitor

```bash
cd /home/atlas/Desktop/ATLAS-Jetson/scripts
MODEL_PATH=/home/atlas/Desktop/ATLAS-Jetson/yolo11s.pt python3 atlas_vision_local.py
```

Abre janela `cv2.imshow`; as teclas `S`, `[`, `]`, `Q` funcionam com a janela OpenCV em foco. Continua a disponibilizar RTSP processado.

## Modo remoto sem monitor

No PC, com o percurso de rede acessível:

```bash
ssh -t atlas@192.168.144.100
```

No Jetson:

```bash
cd /home/atlas/Desktop/ATLAS-Jetson/scripts
MODEL_PATH=/home/atlas/Desktop/ATLAS-Jetson/yolo11s.pt python3 atlas_vision_headless.py
```

Não utiliza X11 nem janela OpenCV. As teclas são lidas com `termios`, `tty` e `select` do terminal SSH. `Q`/Ctrl+C terminam. Se `stdin` não for TTY, o código avisa que as teclas não funcionam.

## Rede original (estado presumido, verificar)

`setup_network.sh` ativa a bridge NetworkManager `atlas-siyi`, aguarda dois adaptadores USB-Ethernet identificados por MAC, ativa os perfis `atlas-siyi-eth2` (A8) e `atlas-siyi-eth1` (HM30), força **10 Mb/s full duplex no interface HM30**, verifica o ping da A8 e cria um proxy TCP `192.168.144.100:37256 → 192.168.144.25:37256` com `socat`.

**Não cria esses três perfis:** presume que foram configurados previamente. Algumas esperas não têm timeout, e alguns loops tentam novamente indefinidamente. Não executar remotamente antes de garantir recuperação de acesso à máquina.

O script termina mostrando o comando da variante **`NOVO_v2.py`**. Para operação SSH sem monitor, usar **`HEADLESS_SSH.py`** como documentado acima. O script da bridge não faz arranque automático de nenhum dos dois.

## Verificação da stream (sem alterar configurações)

```bash
ping -c 2 192.168.144.25
ss -lnt | grep ':8555'
```

A verificação da porta `8555` só é esperada enquanto o script de processamento estiver a servir RTSP.
