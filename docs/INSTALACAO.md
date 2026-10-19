# Requisitos e recuperação do ambiente

Esta documentação descreve os requisitos **observáveis nos scripts**; não é uma receita de instalação comprovada num Jetson novo.

## Hardware

- NVIDIA Jetson Xavier NX com GPU/CUDA funcional.
- SIYI A8 Mini ligada à rede `192.168.144.0/24`.
- SIYI HM30 Ground/Air e as duas interfaces Ethernet pretendidas para a bridge.
- Modelo `yolo11s.pt` recuperado do Desktop original.

## Dependências evidenciadas

**Python:** `opencv-python`/`cv2` com suporte GStreamer; `torch`, `torchvision`, `ultralytics`, `sahi` e bindings `gi` (PyGObject) para `Gst` e `GstRtspServer`.

**Sistema:** GStreamer com `rtspsrc`, `rtph265depay`, `h265parse`, `nvv4l2decoder`, `nvvidconv`, `nvv4l2h264enc`, `h264parse`, `rtph264pay`, `appsrc`; `NetworkManager` (`nmcli`), `ethtool`, `socat`, `ip`, `bridge`, `ping`, `ss`, SSH.

Não executar `pip install -U torch torchvision` às cegas: builds de PyTorch no Xavier NX dependem do JetPack/L4T, e instalações genéricas podem invalidar o suporte CUDA. Não existem versões exatas confirmadas só com os scripts.

## Diagnóstico não destrutivo no Jetson original

```bash
cat /etc/nv_tegra_release
python3 -c 'import torch, torchvision, cv2; print("torch",torch.__version__,"torchvision",torchvision.__version__,"CUDA",torch.cuda.is_available(),"OpenCV",cv2.__version__)'
python3 -c 'import ultralytics, sahi; print("ultralytics",ultralytics.__version__); print("sahi",sahi.__version__)'
gst-inspect-1.0 nvv4l2decoder
gst-inspect-1.0 nvv4l2h264enc
nmcli -f NAME,TYPE,DEVICE connection show
ip -br address
bridge link
systemctl status ssh --no-pager
```

A informação sobre versões deve ser acrescentada só depois de correr os comandos na máquina original.

## Rede e segurança

O `setup_network.sh` **modifica estado de rede** e lança um proxy. Não é um diagnóstico read-only. Antes de o executar, conferir os endereços MAC, a existência dos três perfis, os IPs, a topologia e o acesso de recuperação por ligação física.

O ficheiro `setup_camera_net.sh` da coleção antiga configura apenas uma interface Ethernet, no IP `192.168.144.20/24`, e depende de `atlas_log.sh`; não é chamado pelo `setup_network.sh` e não foi incluído como parte do setup operacional principal.

O proxy `socat` do script original usa TCP em `37256`; deve ser isolado numa rede confiável. Nenhuma autenticação adicional é definida no comando `socat`.

## Limites do que foi validado

- Os ficheiros Python foram preservados sem alterações.
- Verificações sintáticas não comprovam suporte à GPU, velocidade de inferência, latência nem estabilidade em rede real.
- A cópia do Desktop não contém necessariamente serviços de sistema, perfis NetworkManager, permissões ou pacotes instalados.
