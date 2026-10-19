#!/usr/bin/env python3

import os
import time
import threading

import cv2
import torch
import torchvision

from ultralytics import YOLO
from sahi import AutoDetectionModel
from sahi.predict import get_sliced_prediction

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import GLib, Gst, GstRtspServer


# ============================================================
# CONFIGURAÇÃO
# ============================================================

CAMERA_IP = "192.168.144.25"
RTSP_PORT = 8554
STREAM_NAME = "main.264"
STREAM_CODEC = "H265"
RTSP_LATENCY_MS = 150

PASTA_PROJETO = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.environ.get(
    "MODEL_PATH",
    os.path.join(PASTA_PROJETO, "yolo11s.pt"),
)

# Saída RTSP para o vídeo já processado
OUTPUT_RTSP_PORT = os.environ.get("OUTPUT_RTSP_PORT", "8555")
OUTPUT_RTSP_MOUNT = os.environ.get("OUTPUT_RTSP_MOUNT", "/camera")
OUTPUT_RTSP_BITRATE = int(os.environ.get("OUTPUT_RTSP_BITRATE", "1500000"))
OUTPUT_RTSP_IFRAME = int(os.environ.get("OUTPUT_RTSP_IFRAME", "50"))
OUTPUT_FPS = int(os.environ.get("OUTPUT_FPS", "25"))

PERSON_CLASS_ID = 0
CONFIDENCE = 0.25
YOLO_IMGSZ = 640

SAHI_SLICE_SIZE = 640
SAHI_OVERLAP = 0.15
SAHI_EVERY_N = 2
SAHI_BATCH_SIZE = 1

DETECTION_MAX_AGE_S = 1.5


# ============================================================
# WORKAROUND NMS PARA O XAVIER
# ============================================================

_nms_original = torchvision.ops.nms


def nms_cpu(boxes, scores, iou_threshold):
    original_device = boxes.device
    indices = _nms_original(boxes.cpu(), scores.cpu(), iou_threshold)
    return indices.to(original_device)


torchvision.ops.nms = nms_cpu


# ============================================================
# FRAME MAIS RECENTE
# ============================================================

class LatestFrame:
    def __init__(self):
        self.lock = threading.Lock()
        self.frame = None
        self.sequence = 0
        self.timestamp = 0.0

    def set(self, frame):
        with self.lock:
            self.frame = frame
            self.sequence += 1
            self.timestamp = time.time()

    def get(self, copy_frame=False):
        with self.lock:
            if self.frame is None:
                return None, self.sequence, self.timestamp

            frame = self.frame.copy() if copy_frame else self.frame
            return frame, self.sequence, self.timestamp


# ============================================================
# ESTADO DAS DETEÇÕES
# ============================================================

class DetectionState:
    def __init__(self):
        self.lock = threading.Lock()
        self.boxes = []
        self.mode = "NONE"
        self.updated_at = 0.0
        self.inference_ms = 0.0
        self.frame_sequence = -1

    def update(self, boxes, mode, inference_ms, frame_sequence):
        with self.lock:
            self.boxes = boxes
            self.mode = mode
            self.updated_at = time.time()
            self.inference_ms = inference_ms
            self.frame_sequence = frame_sequence

    def get(self):
        with self.lock:
            return (
                list(self.boxes),
                self.mode,
                self.updated_at,
                self.inference_ms,
                self.frame_sequence,
            )


# ============================================================
# GSTREAMER
# ============================================================

def build_gstreamer_pipeline():
    uri = f"rtsp://{CAMERA_IP}:{RTSP_PORT}/{STREAM_NAME}"
    codec = STREAM_CODEC.upper()

    if codec == "H265":
        depay = "rtph265depay"
        parser = "h265parse"
    elif codec == "H264":
        depay = "rtph264depay"
        parser = "h264parse"
    else:
        raise ValueError("STREAM_CODEC tem de ser H264 ou H265")

    return (
        f"rtspsrc location={uri} protocols=tcp latency={RTSP_LATENCY_MS} "
        f"drop-on-latency=true ! {depay} ! {parser} ! nvv4l2decoder ! "
        f"nvvidconv ! video/x-raw,format=BGRx ! videoconvert ! "
        f"video/x-raw,format=BGR ! appsink drop=true max-buffers=1 sync=false"
    )


# ============================================================
# THREAD DE CAPTURA
# ============================================================

class FrameGrabber(threading.Thread):
    def __init__(self, latest_frame):
        super().__init__(daemon=True)
        self.latest_frame = latest_frame
        self.running = True
        self.cap = None

    def stop(self):
        self.running = False

    def run(self):
        pipeline = build_gstreamer_pipeline()

        print("\nPipeline GStreamer:")
        print(pipeline)
        print()

        self.cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)

        if not self.cap.isOpened():
            print("ERRO: não foi possível abrir o stream da A8.")
            print("Verifica IP, codec e stream RTSP.")
            self.running = False
            return

        print("Stream A8 aberto.\n")

        failures = 0

        while self.running:
            ok, frame = self.cap.read()

            if not ok:
                failures += 1
                if failures % 30 == 0:
                    print("Aviso: falha temporária a receber frames.")
                time.sleep(0.01)
                continue

            failures = 0
            self.latest_frame.set(frame)

        self.cap.release()


# ============================================================
# THREAD DE INFERÊNCIA
# ============================================================

class InferenceWorker(threading.Thread):
    def __init__(self, latest_frame, detection_state, yolo_model, sahi_model):
        super().__init__(daemon=True)

        self.latest_frame = latest_frame
        self.detection_state = detection_state
        self.yolo_model = yolo_model
        self.sahi_model = sahi_model
        self.running = True

        self.settings_lock = threading.Lock()
        self.mode = "YOLO"
        self.sahi_every_n = SAHI_EVERY_N
        self.sahi_counter = 0
        self.last_sequence = -1

    def stop(self):
        self.running = False

    def toggle_mode(self):
        with self.settings_lock:
            if self.mode == "YOLO":
                self.mode = "SAHI"
                self.sahi_counter = 0
            else:
                self.mode = "YOLO"
            return self.mode

    def change_sahi_interval(self, delta):
        with self.settings_lock:
            self.sahi_every_n = max(1, self.sahi_every_n + delta)
            self.sahi_counter = 0
            return self.sahi_every_n

    def get_settings(self):
        with self.settings_lock:
            return self.mode, self.sahi_every_n

    def run_yolo(self, frame):
        results = self.yolo_model.predict(
            source=frame,
            imgsz=YOLO_IMGSZ,
            conf=CONFIDENCE,
            classes=[PERSON_CLASS_ID],
            device=0,
            verbose=False,
        )

        boxes = []
        result = results[0]

        if result.boxes is None:
            return boxes

        for box in result.boxes:
            class_id = int(box.cls[0].item())
            if class_id != PERSON_CLASS_ID:
                continue

            confidence = float(box.conf[0].item())
            x1, y1, x2, y2 = box.xyxy[0].detach().cpu().tolist()

            boxes.append(
                {
                    "x1": int(x1),
                    "y1": int(y1),
                    "x2": int(x2),
                    "y2": int(y2),
                    "confidence": confidence,
                    "source": "YOLO",
                }
            )

        return boxes

    def run_sahi(self, frame):
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        result = get_sliced_prediction(
            image=frame_rgb,
            detection_model=self.sahi_model,
            slice_height=SAHI_SLICE_SIZE,
            slice_width=SAHI_SLICE_SIZE,
            overlap_height_ratio=SAHI_OVERLAP,
            overlap_width_ratio=SAHI_OVERLAP,
            perform_standard_pred=False,
            verbose=0,
            batch_size=SAHI_BATCH_SIZE,
        )

        boxes = []

        for prediction in result.object_prediction_list:
            try:
                class_id = int(prediction.category.id)
            except (TypeError, ValueError):
                continue

            if class_id != PERSON_CLASS_ID:
                continue

            x1, y1, x2, y2 = prediction.bbox.to_xyxy()
            confidence = float(prediction.score.value)

            boxes.append(
                {
                    "x1": int(x1),
                    "y1": int(y1),
                    "x2": int(x2),
                    "y2": int(y2),
                    "confidence": confidence,
                    "source": "SAHI",
                }
            )

        return boxes

    def run(self):
        print("Inference worker iniciado.\n")

        while self.running:
            frame, sequence, _ = self.latest_frame.get(copy_frame=False)

            if frame is None:
                time.sleep(0.005)
                continue

            if sequence == self.last_sequence:
                time.sleep(0.001)
                continue

            self.last_sequence = sequence
            mode, sahi_every_n = self.get_settings()

            if mode == "YOLO":
                start = time.perf_counter()
                try:
                    boxes = self.run_yolo(frame)
                    inference_ms = (time.perf_counter() - start) * 1000.0

                    self.detection_state.update(
                        boxes=boxes,
                        mode="YOLO",
                        inference_ms=inference_ms,
                        frame_sequence=sequence,
                    )
                except Exception as exc:
                    print(f"\nERRO YOLO: {exc}\n")
                    time.sleep(0.1)

                continue

            # Modo SAHI: com SAHI_EVERY_N = 2, há inferência
            # numa oportunidade sim, noutra não. Nos intervalos
            # NÃO corre YOLO.
            self.sahi_counter += 1

            if self.sahi_counter % sahi_every_n != 0:
                continue

            start = time.perf_counter()

            try:
                boxes = self.run_sahi(frame)
                inference_ms = (time.perf_counter() - start) * 1000.0

                self.detection_state.update(
                    boxes=boxes,
                    mode="SAHI",
                    inference_ms=inference_ms,
                    frame_sequence=sequence,
                )
            except Exception as exc:
                print(f"\nERRO SAHI: {exc}\n")
                time.sleep(0.1)


# ============================================================
# DESENHAR DETEÇÕES
# ============================================================

def draw_detections(frame, boxes, detection_age):
    if detection_age > DETECTION_MAX_AGE_S:
        return

    for box in boxes:
        x1 = box["x1"]
        y1 = box["y1"]
        x2 = box["x2"]
        y2 = box["y2"]
        confidence = box["confidence"]
        source = box["source"]

        cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)

        text = f"person {confidence:.2f} [{source}]"

        cv2.putText(
            frame,
            text,
            (x1, max(25, y1 - 8)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (0, 255, 0),
            2,
            cv2.LINE_AA,
        )



# ============================================================
# FRAME PROCESSADO MAIS RECENTE
# ============================================================

class LatestProcessedFrame:
    def __init__(self):
        self.lock = threading.Lock()
        self.frame = None
        self.sequence = 0

    def set(self, frame):
        with self.lock:
            self.frame = frame.copy()
            self.sequence += 1

    def get(self):
        with self.lock:
            if self.frame is None:
                return None, self.sequence
            return self.frame.copy(), self.sequence


# ============================================================
# RTSP SERVER
# Esta é a única parte nova relativamente ao teste local:
# pega no MESMO frame que é mostrado localmente, já com as boxes
# YOLO/SAHI e os textos, e transmite-o em H.264.
# ============================================================

class ProcessedFactory(GstRtspServer.RTSPMediaFactory):
    def __init__(self, processed_frame, width, height):
        super().__init__()

        self.processed_frame = processed_frame
        self.width = width
        self.height = height

        self.duration = Gst.SECOND // OUTPUT_FPS
        self.frame_number = 0

        self.appsrc = None
        self.push_thread = None
        self.push_running = False

        launch = (
            "appsrc name=source is-live=true block=false format=time do-timestamp=false "
            f"caps=video/x-raw,format=BGR,width={width},height={height},framerate={OUTPUT_FPS}/1 ! "
            "queue leaky=downstream max-size-buffers=2 ! "
            "videoconvert ! video/x-raw,format=BGRx ! "
            "nvvidconv ! video/x-raw(memory:NVMM),format=NV12 ! "
            f"nvv4l2h264enc bitrate={OUTPUT_RTSP_BITRATE} "
            f"iframeinterval={OUTPUT_RTSP_IFRAME} "
            "insert-sps-pps=1 maxperf-enable=1 ! "
            "h264parse ! "
            "rtph264pay name=pay0 pt=96 config-interval=1"
        )

        self.set_launch(f"( {launch} )")
        self.set_shared(True)
        self.connect("media-configure", self.on_media_configure)

    def on_media_configure(self, factory, media):
        element = media.get_element()
        self.appsrc = element.get_child_by_name("source")
        self.frame_number = 0

        media.connect("unprepared", self.on_media_unprepared)

        if not self.push_running:
            self.push_running = True
            self.push_thread = threading.Thread(
                target=self.push_loop,
                daemon=True,
            )
            self.push_thread.start()

        print("\nCliente RTSP ligado ao vídeo processado.\n")

    def on_media_unprepared(self, media):
        self.push_running = False
        self.appsrc = None
        print("\nCliente RTSP desligado.\n")

    def push_loop(self):
        frame_period = 1.0 / OUTPUT_FPS
        next_deadline = time.perf_counter()

        cached_frame = None
        last_sequence = -1

        while self.push_running:
            now = time.perf_counter()

            if now < next_deadline:
                time.sleep(min(0.002, next_deadline - now))
                continue

            next_deadline += frame_period

            frame, sequence = self.processed_frame.get()

            if frame is not None and sequence != last_sequence:
                cached_frame = frame
                last_sequence = sequence

            if cached_frame is None:
                continue

            out = cached_frame

            if out.shape[1] != self.width or out.shape[0] != self.height:
                out = cv2.resize(out, (self.width, self.height))

            data = out.tobytes()

            buffer = Gst.Buffer.new_allocate(None, len(data), None)
            buffer.fill(0, data)
            buffer.pts = self.frame_number * self.duration
            buffer.dts = buffer.pts
            buffer.duration = self.duration
            self.frame_number += 1

            appsrc = self.appsrc
            if appsrc is None:
                break

            result = appsrc.emit("push-buffer", buffer)

            if result != Gst.FlowReturn.OK:
                print(f"Aviso: push-buffer devolveu {result}.")
                break

        self.push_running = False


def start_rtsp_server(processed_frame, width, height):
    Gst.init(None)

    server = GstRtspServer.RTSPServer()
    server.set_service(OUTPUT_RTSP_PORT)

    factory = ProcessedFactory(
        processed_frame=processed_frame,
        width=width,
        height=height,
    )

    mounts = server.get_mount_points()
    mounts.add_factory(OUTPUT_RTSP_MOUNT, factory)

    server.attach(None)

    # IMPORTANTE:
    # Não criamos uma GLib MainLoop numa segunda thread Python.
    # O OpenCV HighGUI (imshow/waitKey) continua no main thread,
    # exatamente como no jetson_yolo_sahi_local_test.py.
    # O contexto GLib é processado dentro do próprio loop principal.
    glib_context = GLib.MainContext.default()

    print(
        f"RTSP processado: "
        f"rtsp://192.168.144.100:{OUTPUT_RTSP_PORT}{OUTPUT_RTSP_MOUNT}"
    )
    print(
        f"Saída: H264 {OUTPUT_RTSP_BITRATE / 1_000_000:.2f} Mbps, "
        f"{OUTPUT_FPS} FPS, I-frame={OUTPUT_RTSP_IFRAME}\n"
    )

    return server, glib_context


# ============================================================
# MAIN
# ============================================================

def main():
    print("\n==========================================")
    print(" SIYI A8 + YOLO11s / SAHI - Xavier NX")
    print("==========================================\n")

    print("Torch:", torch.__version__)
    print("CUDA:", torch.cuda.is_available())

    if not torch.cuda.is_available():
        print("ERRO: CUDA não está disponível.")
        return

    print("GPU:", torch.cuda.get_device_name(0))
    print("OpenCV:", cv2.__version__)
    print()

    print("A carregar YOLO11s...")
    yolo_model = YOLO(MODEL_PATH)
    print("YOLO11s carregado.")

    print("A preparar SAHI...")
    sahi_model = AutoDetectionModel.from_pretrained(
        model_type="ultralytics",
        model_path=MODEL_PATH,
        model=yolo_model,
        confidence_threshold=CONFIDENCE,
        device="cuda:0",
        image_size=YOLO_IMGSZ,
    )
    print("SAHI preparado.\n")

    latest_frame = LatestFrame()
    detection_state = DetectionState()
    processed_frame = LatestProcessedFrame()

    grabber = FrameGrabber(latest_frame)
    inference_worker = InferenceWorker(
        latest_frame=latest_frame,
        detection_state=detection_state,
        yolo_model=yolo_model,
        sahi_model=sahi_model,
    )

    grabber.start()
    inference_worker.start()

    print("À espera do primeiro frame para iniciar o servidor RTSP...")
    first_frame = None

    while first_frame is None:
        first_frame, _, _ = latest_frame.get(copy_frame=True)

        if first_frame is None:
            if not grabber.running:
                print("Captura terminou antes de chegar o primeiro frame.")
                return
            time.sleep(0.01)

    input_height, input_width = first_frame.shape[:2]

    rtsp_server, glib_context = start_rtsp_server(
        processed_frame=processed_frame,
        width=input_width,
        height=input_height,
    )

    print("Modo inicial: YOLO\n")
    print("Controlos:")
    print("S -> alternar YOLO / SAHI")
    print("[ -> SAHI mais frequente")
    print("] -> SAHI menos frequente")
    print("Q -> sair\n")
    print(f"SAHI inicial: 1 inferência a cada {SAHI_EVERY_N} oportunidades.\n")

    last_display_sequence = -1
    last_display_time = time.perf_counter()
    display_fps = 0.0

    try:
        while True:
            # Processa os eventos do servidor RTSP no mesmo main thread.
            # Isto evita pôr GLib e OpenCV/X11 em main loops Python separados.
            while glib_context.pending():
                glib_context.iteration(False)

            frame, sequence, _ = latest_frame.get(copy_frame=True)

            if frame is None:
                if not grabber.running:
                    print("Captura terminou.")
                    break
                time.sleep(0.01)
                continue

            if sequence == last_display_sequence:
                time.sleep(0.001)
                continue

            last_display_sequence = sequence

            (
                boxes,
                detection_mode,
                detection_timestamp,
                inference_ms,
                _,
            ) = detection_state.get()

            if detection_timestamp > 0:
                detection_age = time.time() - detection_timestamp
            else:
                detection_age = 999.0

            draw_detections(frame, boxes, detection_age)

            now = time.perf_counter()
            delta = now - last_display_time
            last_display_time = now

            if delta > 0:
                instantaneous_fps = 1.0 / delta
                if display_fps == 0:
                    display_fps = instantaneous_fps
                else:
                    display_fps = 0.9 * display_fps + 0.1 * instantaneous_fps

            current_mode, sahi_every_n = inference_worker.get_settings()

            cv2.putText(
                frame,
                f"Modo: {current_mode}",
                (20, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )

            cv2.putText(
                frame,
                f"Video: {display_fps:.1f} FPS",
                (20, 60),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )

            cv2.putText(
                frame,
                f"Ultima inferencia: {detection_mode} {inference_ms:.0f} ms",
                (20, 90),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )

            frequency_text = (
                f"SAHI: 1 em cada {sahi_every_n}"
                if current_mode == "SAHI"
                else "YOLO: continuo"
            )

            cv2.putText(
                frame,
                frequency_text,
                (20, 120),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )

            cv2.putText(
                frame,
                f"Deteccoes: {len(boxes)}",
                (20, 150),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )

            # O RTSP recebe exatamente este mesmo frame já processado.
            processed_frame.set(frame)

            cv2.imshow("A8 - YOLO11s / SAHI", frame)
            key = cv2.waitKey(1) & 0xFF

            if key in (ord("q"), ord("Q")):
                break

            elif key in (ord("s"), ord("S")):
                new_mode = inference_worker.toggle_mode()
                print(f"\nModo alterado para: {new_mode}\n")

            elif key == ord("["):
                interval = inference_worker.change_sahi_interval(-1)
                print(f"SAHI agora: 1 em cada {interval}.")

            elif key == ord("]"):
                interval = inference_worker.change_sahi_interval(1)
                print(f"SAHI agora: 1 em cada {interval}.")

    except KeyboardInterrupt:
        pass

    finally:
        print("\nA terminar...")
        grabber.stop()
        inference_worker.stop()

        grabber.join(timeout=2.0)
        inference_worker.join(timeout=2.0)
        cv2.destroyAllWindows()
        print("Terminado.")


if __name__ == "__main__":
    main()
