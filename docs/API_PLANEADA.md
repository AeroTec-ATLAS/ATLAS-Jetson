# Próxima fase: API local Jetson ↔ ATLAS Video Control

**Estado: proposta de arquitetura. Não está implementada nos scripts atuais.**

Objetivo: alterar os modos e parâmetros da deteção a partir do ATLAS sem depender das teclas numa sessão SSH, mantendo a descodificação e retransmissão RTSP existentes.

## Funções candidatas

- `GET /api/v1/status`: serviço, stream, GPU e modo de deteção.
- `GET /api/v1/ai/settings`: modo YOLO/SAHI, frequência SAHI, confiança.
- `PUT /api/v1/ai/settings`: atualização validada dos parâmetros suportados.
- `POST /api/v1/ai/start` e `/stop`: iniciar/parar processamento sob supervisor independente do processo principal.
- `GET /api/v1/ai/detections`: deteções mais recentes com timestamps, dimensões do frame e confiança.
- `GET /api/v1/health`: disponibilidade e falhas recentes.

São **contratos a decidir**, não endpoints já existentes. `click-to-track`, controlo da gimbal, autogeolocalização e identificação persistente de alvos pertencem a fases posteriores.

## Regras de integração

1. Preservar as pipelines RTSP atuais enquanto testamos a API separadamente.
2. Não depender de SSH interativo para controlar funcionalidades da IA.
3. Definir autenticação/ligação à rede privada HM30; não expor controlo sem autenticação na Internet.
4. Usar timestamps e IDs de frame para alinhar eventos de deteção e vídeo, e distinguir deteções obsoletas.
5. O controlador de voo Pixhawk não recebe comandos desta API.
6. Implementar funções de desligar IA/retornar ao modo manual e testar desconexões antes de ligar o controlo automático da gimbal.
7. Escolher formalmente se o endpoint correrá no mesmo processo Python ou num serviço supervisor, depois de medir estabilidade.

Não são feitos commits de implementação da API nesta recuperação.
