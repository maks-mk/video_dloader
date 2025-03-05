import sys
import os
import json
import re
import logging
from datetime import datetime
from typing import Tuple, List, Dict, Any, Optional, Set
import subprocess
import shutil
import threading

from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QLabel, QLineEdit, QPushButton,
                             QComboBox, QProgressBar, QListWidget, QFrame,
                             QRadioButton, QButtonGroup, QMessageBox, QStyle)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QObject, QRunnable, QThreadPool
from PyQt6.QtGui import QIcon, QFont, QKeySequence, QShortcut, QPixmap, QCursor
import yt_dlp

# Настройка логирования
log_dir: str = "logs"
os.makedirs(log_dir, exist_ok=True)
current_date: str = datetime.now().strftime("%Y-%m-%d")
log_file: str = os.path.join(log_dir, f"video_downloader_{current_date}.log")
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(funcName)s(%(lineno)d): %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('VideoDownloader')

# Функция для получения пути к ресурсам, корректно работающая с PyInstaller
def get_resource_path(relative_path: str) -> str:
    """
    Получает абсолютный путь к ресурсу, корректно работает как в режиме разработки,
    так и в скомпилированном PyInstaller EXE.
    """
    try:
        # PyInstaller создает временную директорию и сохраняет путь в _MEIPASS
        base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base_path, relative_path)
    except Exception as e:
        logger.error(f"Ошибка при определении пути ресурса {relative_path}: {e}")
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)

def get_service_name(url: str) -> str:
    """
    Определяет название видеосервиса по URL.
    """
    if 'youtube.com' in url or 'youtu.be' in url:
        return 'YouTube'
    elif 'vk.com' in url or 'vkvideo.ru' in url:
        return 'VK'
    elif 'rutube.ru' in url:
        return 'RuTube'
    elif 'ok.ru' in url:
        return 'Одноклассники'
    elif 'mail.ru' in url:
        return 'Mail.ru'
    return 'Неизвестный сервис'

# Константы с паттернами URL для разных сервисов
URL_PATTERNS = {
    'YouTube': [
        r'^https?://(?:www\.)?youtube\.com/watch\?v=[\w-]{11}(?:&\S*)?$',
        r'^https?://youtu\.be/[\w-]{11}(?:\?\S*)?$',
        r'^https?://(?:www\.)?youtube\.com/shorts/[\w-]{11}(?:\?\S*)?$',
        r'^https?://(?:www\.)?youtube\.com/embed/[\w-]{11}(?:\?\S*)?$'
    ],
    'VK': [
        r'^https?://(?:www\.)?vk\.com/video-?\d+_\d+(?:\?\S*)?$',
        r'^https?://(?:www\.)?vkvideo\.ru/video-?\d+_\d+(?:\?\S*)?$'
    ],
    'RuTube': [
        r'^https?://(?:www\.)?rutube\.ru/video/[\w-]{32}/?(?:\?\S*)?$',
        r'^https?://(?:www\.)?rutube\.ru/play/embed/[\w-]{32}/?(?:\?\S*)?$'
    ],
    'Одноклассники': [
        r'^https?://(?:www\.)?ok\.ru/video/\d+(?:\?\S*)?$'
    ],
    'Mail.ru': [
        r'^https?://(?:www\.)?my\.mail\.ru/(?:[\w/]+/)?video/(?:[\w/]+/)\d+\.html(?:\?\S*)?$'
    ]
}

class ResolutionWorker(QThread):
    resolutions_found = pyqtSignal(list)
    error_occurred = pyqtSignal(str)

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url: str = url

    def run(self) -> None:
        try:
            logger.info(f"Получение доступных разрешений для: {self.url}")
            ydl_opts: Dict[str, Any] = {'quiet': True, 'no_warnings': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info: Dict[str, Any] = ydl.extract_info(self.url, download=False)
                formats: List[Dict[str, Any]] = info.get('formats', [])
                # Собираем разрешения из доступных форматов
                resolutions: Set[str] = {f"{fmt['height']}p" for fmt in formats
                                          if fmt.get('height') and fmt.get('vcodec') != 'none'}
                if not resolutions:
                    resolutions = {'720p'}
                # Сортировка разрешений по убыванию
                sorted_resolutions: List[str] = sorted(list(resolutions),
                                                       key=lambda x: int(x.replace('p', '')),
                                                       reverse=True)
            logger.info(f"Найдены разрешения: {sorted_resolutions}")
            self.resolutions_found.emit(sorted_resolutions)
        except Exception as e:
            logger.exception(f"Ошибка при получении разрешений: {self.url}")
            user_friendly_error = "Не удалось получить доступные разрешения. Проверьте URL и подключение к интернету."
            self.error_occurred.emit(user_friendly_error)

# Реализация QRunnable для работы с QThreadPool
class DownloadRunnable(QRunnable):
    class Signals(QObject):
        progress = pyqtSignal(str, float)
        finished = pyqtSignal(bool, str, str)
        
    def __init__(self, url: str, mode: str, resolution: Optional[str] = None,
                 output_dir: str = 'downloads') -> None:
        super().__init__()
        self.url = url
        self.mode = mode
        self.resolution = resolution
        self.output_dir = output_dir
        self.signals = self.Signals()
        self.cancel_event = threading.Event()
        self.downloaded_filename = None
        
        os.makedirs(output_dir, exist_ok=True)
        
    def run(self) -> None:
        try:
            logger.info(f"Начало загрузки (QRunnable): {self.url}")
            if self.mode == 'video':
                success = self.download_video()
            else:
                success = self.download_audio()

            if success:
                logger.info(f"Загрузка завершена успешно: {self.url}")
                self.signals.finished.emit(True, "Загрузка завершена", self.downloaded_filename or "")
            else:
                logger.info(f"Загрузка отменена: {self.url}")
                self.signals.finished.emit(False, "Загрузка отменена", "")
        except Exception as e:
            logger.exception(f"Ошибка загрузки: {self.url}")
            error_message = self.get_user_friendly_error_message(str(e))
            self.signals.finished.emit(False, error_message, "")
            
    def get_user_friendly_error_message(self, error: str) -> str:
        """Преобразует технические сообщения об ошибках в понятные для пользователя"""
        if "HTTP Error 404" in error:
            return "Ошибка: Видео не найдено (404). Возможно, оно было удалено или является приватным."
        elif "HTTP Error 403" in error:
            return "Ошибка: Доступ запрещен (403). Видео может быть недоступно в вашем регионе."
        elif "Sign in to confirm your age" in error or "age-restricted" in error:
            return "Ошибка: Видео имеет возрастные ограничения и требует авторизации."
        elif "SSL" in error or "подключени" in error.lower() or "connect" in error.lower():
            return "Ошибка подключения. Проверьте соединение с интернетом или попробуйте позже."
        elif "copyright" in error.lower() or "copyright infringement" in error:
            return "Ошибка: Видео недоступно из-за нарушения авторских прав."
        else:
            return f"Ошибка загрузки: {error}"
            
    def download_video(self) -> bool:
        try:
            if not self.resolution:
                raise Exception("Не указано разрешение для видео")
            resolution_number: str = self.resolution.replace('p', '')
            service: str = get_service_name(self.url)
            logger.info(f"Загрузка видео с {service} в разрешении {resolution_number}p")

            ydl_opts: Dict[str, Any] = {
                'format': f'bestvideo[height<={resolution_number}]+bestaudio/best[height<={resolution_number}]',
                'merge_output_format': 'mp4',
                'outtmpl': os.path.join(self.output_dir, '%(title)s_%(resolution)s.%(ext)s'),
                'progress_hooks': [self.progress_hook],
                'postprocessors': [{
                    'key': 'FFmpegVideoConvertor',
                    'preferedformat': 'mp4',
                }],
                'socket_timeout': 30,
                'retries': 10,
                'fragment_retries': 10,
                'retry_sleep': 3,
                'ignoreerrors': True,
                'no_warnings': True,
                'quiet': True,
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.params['resolution'] = self.resolution
                ydl.download([self.url])
            return True

        except Exception as e:
            logger.exception(f"Ошибка загрузки видео")
            raise
            
    def download_audio(self) -> bool:
        try:
            ydl_opts: Dict[str, Any] = {
                'format': 'bestaudio/best',
                'outtmpl': os.path.join(self.output_dir, '%(title)s_audio.%(ext)s'),
                'progress_hooks': [self.progress_hook],
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([self.url])
            return True

        except Exception as e:
            logger.exception(f"Ошибка загрузки аудио")
            raise
            
    def progress_hook(self, d: Dict[str, Any]) -> None:
        if self.cancel_event.is_set():
            raise Exception("Загрузка отменена пользователем")

        if d.get('status') == 'downloading':
            try:
                downloaded: float = d.get('downloaded_bytes', 0)
                total: float = d.get('total_bytes', 0) or d.get('total_bytes_estimate', 0)
                if total:
                    percent: float = (downloaded / total) * 100
                    self.signals.progress.emit(f"Загрузка: {percent:.1f}%", percent)
                else:
                    # Если размер неизвестен, отправляем неопределенный прогресс
                    self.signals.progress.emit("Загрузка...", -1)
            except Exception as e:
                logger.exception("Ошибка в progress_hook")
        elif d.get('status') == 'finished':
            self.downloaded_filename = os.path.basename(d.get('filename', ''))
            self.signals.progress.emit("Обработка файла...", 100)
            
    def cancel(self) -> None:
        self.cancel_event.set()
        logger.info(f"Запрошена отмена загрузки: {self.url}")

# Функция для загрузки изображений для многократного использования
def load_image(image_name: str, size: Tuple[int, int] = (100, 100)) -> Tuple[bool, Optional[QPixmap], str]:
    """
    Загружает изображение с проверкой различных расширений.
    
    Args:
        image_name: Имя файла без расширения
        size: Размер для масштабирования (ширина, высота)
        
    Returns:
        Tuple из (успех загрузки, pixmap или None, путь к файлу)
    """
    # Изменяем порядок расширений, чтобы PNG был первым
    extensions = [".png", ".jpeg", ".jpg", ".gif", ".ico"]
    
    for ext in extensions:
        image_path = get_resource_path(f"{image_name}{ext}")
        if os.path.exists(image_path):
            try:
                pixmap = QPixmap(image_path)
                if not pixmap.isNull():
                    # Масштабируем изображение до указанного размера
                    scaled_pixmap = pixmap.scaled(size[0], size[1], Qt.AspectRatioMode.KeepAspectRatio, 
                                             Qt.TransformationMode.SmoothTransformation)
                    logger.info(f"Изображение успешно загружено: {image_path}")
                    return True, scaled_pixmap, image_path
                else:
                    logger.warning(f"Изображение не удалось загрузить (пустой pixmap): {image_path}")
            except Exception as e:
                logger.exception(f"Ошибка при загрузке изображения {image_path}")
    
    logger.warning(f"Изображение {image_name} не найдено ни с одним из поддерживаемых расширений")
    return False, None, ""

# Функция специально для загрузки логотипа, которая всегда использует png
def load_logo(size: Tuple[int, int] = (80, 80)) -> Tuple[bool, Optional[QPixmap], str]:
    """
    Загружает логотип в формате PNG.
    
    Args:
        size: Размер для масштабирования (ширина, высота)
        
    Returns:
        Tuple из (успех загрузки, pixmap или None, путь к файлу)
    """
    image_path = get_resource_path("vid1.png")
    logger.info(f"Загрузка логотипа из: {image_path}")
    
    if os.path.exists(image_path):
        try:
            pixmap = QPixmap(image_path)
            if not pixmap.isNull():
                scaled_pixmap = pixmap.scaled(size[0], size[1], Qt.AspectRatioMode.KeepAspectRatio, 
                                        Qt.TransformationMode.SmoothTransformation)
                logger.info(f"Логотип успешно загружен: {image_path}")
                return True, scaled_pixmap, image_path
            else:
                logger.warning(f"Логотип не удалось загрузить (пустой pixmap): {image_path}")
        except Exception as e:
            logger.exception(f"Ошибка при загрузке логотипа: {image_path}")
    else:
        logger.warning(f"Файл логотипа не найден: {image_path}")
    
    return False, None, ""

class VideoDownloaderUI(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Video Downloader")
        self.setMinimumSize(950, 600)
        
        # Установка иконки приложения
        self.setup_app_icon()
        
        # Инициализация пула потоков
        self.thread_pool = QThreadPool()
        logger.info(f"Максимальное количество потоков: {self.thread_pool.maxThreadCount()}")
        
        central_widget: QWidget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QHBoxLayout(central_widget)

        # Левая и правая панели
        left_panel: QFrame = QFrame()
        right_panel: QFrame = QFrame()
        left_panel.setFrameStyle(QFrame.Shape.StyledPanel)
        right_panel.setFrameStyle(QFrame.Shape.StyledPanel)
        left_layout = QVBoxLayout(left_panel)
        right_layout = QVBoxLayout(right_panel)

        # Заголовки
        title_label: QLabel = QLabel("Video Downloader")
        title_label.setFont(QFont("Arial", 24, QFont.Weight.Bold))
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet("color: #2196F3; padding: 5px; margin: 5px 0 10px 0;")

        subtitle_label: QLabel = QLabel("Скачивай видео с YouTube, VK, Rutube, Mail.ru, OK")
        subtitle_label.setFont(QFont("Arial", 12))
        subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle_label.setStyleSheet("color: #666666; padding: 5px; margin: 0 0 10px 0;")

        separator: QFrame = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setStyleSheet("background-color: #ddd; margin: 5px 0;")

        # Поле ввода URL и кнопка "Вставить"
        url_layout: QHBoxLayout = QHBoxLayout()
        self.url_input: QLineEdit = QLineEdit()
        self.url_input.setPlaceholderText("Вставьте URL видео...")
        paste_button: QPushButton = QPushButton("Вставить (Ctrl+V)")
        paste_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton))
        url_layout.addWidget(self.url_input)
        url_layout.addWidget(paste_button)

        # Выбор режима загрузки
        mode_group: QButtonGroup = QButtonGroup(self)
        mode_layout: QHBoxLayout = QHBoxLayout()
        self.video_radio: QRadioButton = QRadioButton("Видео (MP4)")
        self.audio_radio: QRadioButton = QRadioButton("Аудио (MP3)")
        mode_group.addButton(self.video_radio)
        mode_group.addButton(self.audio_radio)
        mode_layout.addWidget(self.video_radio)
        mode_layout.addWidget(self.audio_radio)

        # Выбор разрешения
        self.resolution_layout: QHBoxLayout = QHBoxLayout()
        self.resolution_combo: QComboBox = QComboBox()
        refresh_button: QPushButton = QPushButton("Обновить")
        refresh_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.resolution_layout.addWidget(QLabel("Разрешение:"))
        self.resolution_layout.addWidget(self.resolution_combo)
        self.resolution_layout.addWidget(refresh_button)

        # Прогресс загрузки
        self.progress_bar: QProgressBar = QProgressBar()
        self.status_label: QLabel = QLabel("Ожидание...")
        self.status_label.setStyleSheet("color: #666666;")

        # Кнопки управления загрузкой
        buttons_layout: QHBoxLayout = QHBoxLayout()
        add_button: QPushButton = QPushButton("Добавить в очередь (Ctrl+D)")
        add_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder))
        cancel_button: QPushButton = QPushButton("Отменить")
        cancel_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogCancelButton))
        start_button: QPushButton = QPushButton("Загрузить все (Ctrl+S)")
        start_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        buttons_layout.addWidget(add_button)
        buttons_layout.addWidget(cancel_button)
        buttons_layout.addWidget(start_button)

        # Кнопки управления очередью (размещаем только в правой панели)
        queue_buttons_layout: QHBoxLayout = QHBoxLayout()
        clear_queue_button: QPushButton = QPushButton("Очистить очередь")
        clear_queue_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon))
        clear_queue_button.clicked.connect(self.clear_queue)
        remove_selected_button: QPushButton = QPushButton("Удалить выбранное")
        remove_selected_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogDiscardButton))
        remove_selected_button.clicked.connect(self.remove_selected)
        queue_buttons_layout.addWidget(clear_queue_button)
        queue_buttons_layout.addWidget(remove_selected_button)

        # Очередь загрузок
        self.queue_list: QListWidget = QListWidget()
        self.queue_list.setMinimumWidth(300)
        self.queue_list.setMinimumHeight(400)
        queue_label: QLabel = QLabel("Очередь загрузок")
        queue_label.setFont(QFont("Arial", 12, QFont.Weight.Bold))

        # Информация о контактах
        contact_layout: QVBoxLayout = QVBoxLayout()
        contact_layout.setSpacing(0)
        contact_layout.setContentsMargins(0, 0, 0, 0)

        email_label: QLabel = QLabel("maks_k77@mail.ru")
        email_label.setStyleSheet("color: #A52A2A; font-weight: bold; margin: 0px; padding: 0px;")

        donate_label: QLabel = QLabel("donate: Т-Банк   2200 7001 2147 7888")
        donate_label.setStyleSheet("color: #4169E1; font-weight: bold; margin: 0px; padding: 0px;")

        # Добавляем изображение с обработчиком события
        logo_layout = QHBoxLayout()
        self.logo_label = QLabel()
        self.logo_label.setMinimumSize(64, 64)
        
        # Загружаем логотип с помощью специальной функции для PNG
        success, pixmap, _ = load_logo((80, 80))
        if success:
            self.logo_label.setPixmap(pixmap)
        else:
            # Если не удалось загрузить PNG, явно проверяем другие расширения
            success, pixmap, _ = load_image("vid1", (80, 80))
            if success:
                self.logo_label.setPixmap(pixmap)
            else:
                # Если изображение не найдено, показываем текст
                self.logo_label.setText("О программе")
                self.logo_label.setStyleSheet("color: blue; text-decoration: underline;")
        
        # Устанавливаем курсор и подсказку
        self.logo_label.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.logo_label.setToolTip("Нажмите, чтобы увидеть информацию о программе")
        self.logo_label.mousePressEvent = self.show_about_dialog
        
        # Создаем рамку для выделения области с логотипом
        logo_frame = QFrame()
        logo_frame_layout = QVBoxLayout(logo_frame)
        logo_frame_layout.setContentsMargins(5, 5, 5, 5)  # Уменьшаем внутренние отступы
        logo_frame_layout.addWidget(self.logo_label, 0, Qt.AlignmentFlag.AlignCenter)  # Выравниваем по центру
        logo_frame.setFrameShape(QFrame.Shape.StyledPanel)
        logo_frame.setStyleSheet("background-color: #f0f0f0; border-radius: 5px;")
        
        logo_layout.addWidget(logo_frame)
        
        # Нижний блок левой панели - центрирование логотипа
        bottom_layout = QVBoxLayout()
        
        # Добавляем логотип внизу и выравниваем его по центру
        logo_container = QHBoxLayout()
        logo_container.addStretch()  # Растяжка слева от логотипа
        logo_container.addLayout(logo_layout)
        logo_container.addStretch()  # Растяжка справа от логотипа
        bottom_layout.addLayout(logo_container)

        # Сборка левой панели
        left_layout.addWidget(title_label)
        left_layout.addWidget(subtitle_label)
        left_layout.addWidget(separator)
        left_layout.addLayout(url_layout)
        left_layout.addLayout(mode_layout)
        left_layout.addLayout(self.resolution_layout)
        left_layout.addWidget(self.progress_bar)
        left_layout.addWidget(self.status_label)
        left_layout.addLayout(buttons_layout)
        left_layout.addStretch()
        left_layout.addLayout(bottom_layout)  # Заменяем отдельные виджеты на bottom_layout

        # Сборка правой панели
        right_layout.addWidget(queue_label)
        right_layout.addWidget(self.queue_list)
        right_layout.addLayout(queue_buttons_layout)

        # Добавляем панели в основной layout
        main_layout.addWidget(left_panel, 2)
        main_layout.addWidget(right_panel, 1)

        # Стилизация приложения
        self.setStyleSheet("""
            QMainWindow { background-color:rgb(208, 203, 223); }
            QFrame { background-color: rgb(245, 242, 231); border-radius: 10px; padding: 20px; }
            QPushButton {
                background-color: #2196F3;
                color: white;
                border: none;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #1976D2; }
            QLineEdit {
                padding: 8px;
                border: 1px solid #ddd;
                border-radius: 4px;
            }
            QProgressBar {
                border: 1px solid #ddd;
                border-radius: 4px;
                text-align: center;
            }
            QProgressBar::chunk { background-color: #4CAF50; }
        """)

        # Инициализация переменных
        self.current_download: Optional[DownloadRunnable] = None
        self.download_queue: List[Dict[str, Any]] = []
        self.successful_downloads: List[tuple] = []
        self.failed_downloads: List[tuple] = []
        self.settings: Dict[str, Any] = self.load_settings()

        # Подключение сигналов
        paste_button.clicked.connect(self.paste_url)
        add_button.clicked.connect(self.add_to_queue)
        cancel_button.clicked.connect(self.cancel_download)
        refresh_button.clicked.connect(self.update_resolutions)
        start_button.clicked.connect(self.start_downloads)
        self.video_radio.toggled.connect(self.on_mode_changed)

        # Горячие клавиши
        QShortcut(QKeySequence("Ctrl+V"), self).activated.connect(self.paste_url)
        QShortcut(QKeySequence("Ctrl+D"), self).activated.connect(self.add_to_queue)
        QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(self.start_downloads)

        # Применяем настройки из файла
        self.apply_settings()

    def setup_app_icon(self) -> None:
        """
        Устанавливает иконку приложения из файла изображения.
        Если файл не найден, используется стандартная иконка.
        """
        # Используем функцию для загрузки логотипа в формате PNG
        success, pixmap, image_path = load_logo((32, 32))
        if success:
            app_icon = QIcon(pixmap)
            self.setWindowIcon(app_icon)
            logger.info(f"Установлена иконка приложения из: {image_path}")
        else:
            logger.warning("Файл логотипа для иконки приложения не найден")

    def load_settings(self) -> Dict[str, Any]:
        try:
            if os.path.exists('settings.json'):
                with open('settings.json', 'r', encoding='utf-8') as f:
                    settings = json.load(f)
                    logger.info("Настройки успешно загружены")
                    return settings
        except Exception as e:
            logger.error(f"Ошибка загрузки настроек: {e}")
        return {"download_mode": "video", "last_resolution": "720p"}

    def save_settings(self) -> None:
        try:
            settings = {
                "download_mode": "video" if self.video_radio.isChecked() else "audio",
                "last_resolution": self.resolution_combo.currentText()
            }
            with open('settings.json', 'w', encoding='utf-8') as f:
                json.dump(settings, f)
            logger.info("Настройки сохранены")
        except Exception as e:
            logger.error(f"Ошибка сохранения настроек: {e}")

    def apply_settings(self) -> None:
        """
        Применяет загруженные настройки (например, выбор режима загрузки и последний выбор разрешения).
        """
        mode: str = self.settings.get("download_mode", "video")
        if mode == "audio":
            self.audio_radio.setChecked(True)
        else:
            self.video_radio.setChecked(True)
        # Если режим видео и есть сохранённое разрешение, устанавливаем его (после получения доступных разрешений)
        # Здесь можно добавить дополнительную логику для установки разрешения

    def paste_url(self) -> None:
        clipboard = QApplication.clipboard()
        url: str = clipboard.text().strip()

        is_valid, error_message = self.is_valid_video_url(url)
        if not is_valid:
            logger.warning(f"Попытка вставить некорректный URL: {url}. Причина: {error_message}")
            QMessageBox.warning(self, "Ошибка", error_message)
            return

        self.url_input.setText(url)
        logger.info(f"URL вставлен из буфера обмена: {url}")

        if self.video_radio.isChecked():
            self.update_resolutions()

    def update_resolutions(self) -> None:
        """
        Получает доступные разрешения в отдельном потоке для повышения отзывчивости UI.
        """
        url: str = self.url_input.text().strip()
        if not url or not url.startswith(('http://', 'https://')):
            return

        self.resolution_combo.clear()
        self.resolution_combo.addItem("Получение разрешений...")
        self.resolution_combo.setEnabled(False)
        self.status_label.setText("Получение доступных разрешений...")
        self.status_label.setStyleSheet("color: #2196F3;")
        QApplication.processEvents()

        self.resolution_worker = ResolutionWorker(url)
        self.resolution_worker.resolutions_found.connect(self.on_resolutions_found)
        self.resolution_worker.error_occurred.connect(self.on_resolutions_error)
        self.resolution_worker.start()

    def on_resolutions_found(self, sorted_resolutions: List[str]) -> None:
        self.resolution_combo.clear()
        self.resolution_combo.addItems(sorted_resolutions)
        self.resolution_combo.setEnabled(True)
        self.status_label.setText("Разрешения обновлены")
        self.status_label.setStyleSheet("color: green;")
        # Если сохранённое разрешение присутствует в списке, устанавливаем его
        last_resolution = self.settings.get("last_resolution")
        if last_resolution in sorted_resolutions:
            index = sorted_resolutions.index(last_resolution)
            self.resolution_combo.setCurrentIndex(index)

    def on_resolutions_error(self, error_msg: str) -> None:
        self.resolution_combo.clear()
        self.resolution_combo.addItem("720p")
        self.resolution_combo.setEnabled(True)
        self.status_label.setText(f"Ошибка: {error_msg}")
        self.status_label.setStyleSheet("color: red;")

    def add_to_queue(self) -> None:
        url: str = self.url_input.text().strip()
        is_valid, error_message = self.is_valid_video_url(url)

        if not is_valid:
            logger.warning(f"Некорректный URL: {url}. Причина: {error_message}")
            QMessageBox.warning(self, "Ошибка", error_message)
            return

        mode: str = "video" if self.video_radio.isChecked() else "audio"
        resolution: Optional[str] = self.resolution_combo.currentText() if mode == "video" else None
        service: str = get_service_name(url)
        logger.info(f"Добавление в очередь: {url}, сервис: {service}, режим: {mode}")

        self.download_queue.append({
            'url': url,
            'mode': mode,
            'resolution': resolution,
            'service': service
        })
        self.update_queue_display()
        self.url_input.clear()
        self.save_settings()

    def update_queue_display(self) -> None:
        self.queue_list.clear()
        for i, item in enumerate(self.download_queue, 1):
            mode_text: str = f"видео ({item['resolution']})" if item['mode'] == "video" else "аудио"
            if (self.current_download and
                self.current_download.url == item['url'] and
                self.current_download.mode == item['mode'] and
                (self.current_download.mode == 'audio' or
                 self.current_download.resolution == item['resolution'])):
                prefix: str = "⌛"
            else:
                prefix = " "
            self.queue_list.addItem(
                f"{prefix} {i}. [{item.get('service', 'Неизвестный сервис')}] {item['url']} - {mode_text}"
            )

    def start_downloads(self) -> None:
        if not self.download_queue:
            QMessageBox.information(self, "Информация", "Очередь загрузок пуста")
            return
        # Отключаем кнопки добавления и старта, чтобы предотвратить повторные нажатия
        self.set_controls_enabled(False)
        if self.current_download is None:
            logger.info("Запуск очереди загрузок")
            self.process_queue()

    def process_queue(self) -> None:
        if not self.download_queue:
            self.status_label.setText("Все загрузки завершены")
            self.status_label.setStyleSheet("color: green;")
            self.progress_bar.setValue(0)
            logger.info("Очередь загрузок завершена")
            self.set_controls_enabled(True)
            return

        download: Dict[str, Any] = self.download_queue[0]
        logger.info(f"Начало загрузки: {download['url']}, режим: {download['mode']}")

        # Используем DownloadRunnable вместо DownloadThread с ThreadPool
        download_runnable = DownloadRunnable(
            download['url'],
            download['mode'],
            download['resolution']
        )
        
        # Подключаем сигналы
        download_runnable.signals.progress.connect(self.update_progress)
        download_runnable.signals.finished.connect(self.on_download_finished)
        
        # Сохраняем ссылку на текущую загрузку
        self.current_download = download_runnable
        self.update_queue_display()
        
        # Запускаем загрузку в пуле потоков
        self.thread_pool.start(download_runnable)

    def update_progress(self, status: str, percent: float) -> None:
        self.status_label.setText(status)
        if percent >= 0:
            self.progress_bar.setValue(int(percent))
        else:
            # Если процент отрицательный, показываем неопределенный прогресс
            self.progress_bar.setRange(0, 0)
        # Уменьшаем частоту вызовов processEvents, чтобы не нарушать основной цикл событий
        # Вызываем только раз в 5 обновлений
        self.progress_update_counter = getattr(self, 'progress_update_counter', 0) + 1
        if self.progress_update_counter % 5 == 0:
            QApplication.processEvents()

    def on_download_finished(self, success: bool, message: str, filename: str) -> None:
        if success:
            self.status_label.setStyleSheet("color: green;")
            logger.info(f"Загрузка завершена успешно: {message}")
            if self.current_download and filename:
                self.successful_downloads.append((filename, self.current_download.url))
        else:
            self.status_label.setStyleSheet("color: red;")
            logger.error(f"Ошибка загрузки: {message}")
            if self.current_download:
                self.failed_downloads.append((self.current_download.url, message))

        self.status_label.setText(message)

        if self.download_queue:
            self.download_queue.pop(0)

        self.current_download = None
        self.update_queue_display()

        if not self.download_queue:
            self.show_download_summary()
            self.set_controls_enabled(True)
        else:
            self.process_queue()

    def show_download_summary(self) -> None:
        if not self.successful_downloads and not self.failed_downloads:
            return
        message: str = "Результаты загрузки:\n\n"
        if self.successful_downloads:
            message += "Успешно загружены:\n"
            for filename, _ in self.successful_downloads:
                if filename.endswith('.webm'):
                    if '_audio' in filename:
                        filename = filename.replace('.webm', '.mp3')
                    else:
                        filename = filename.replace('.webm', '.mp4')
                message += f"✓ {filename}\n"
        if self.failed_downloads:
            message += "\nНе удалось загрузить:\n"
            for url, error in self.failed_downloads:
                short_url: str = url if len(url) <= 50 else url[:50] + "..."
                message += f"✗ {short_url}\n   Причина: {error}\n"
        self.cleanup_temp_files()
        QMessageBox.information(self, "Загрузка завершена", message)
        self.successful_downloads.clear()
        self.failed_downloads.clear()

    def cleanup_temp_files(self) -> None:
        """
        Очищает временные файлы в папке загрузок.
        """
        try:
            downloads_dir: str = self.current_download.output_dir if self.current_download else 'downloads'
            if os.path.exists(downloads_dir):
                for file in os.listdir(downloads_dir):
                    if file.endswith(('.part', '.ytdl')):
                        full_path: str = os.path.join(downloads_dir, file)
                        try:
                            os.remove(full_path)
                            logger.info(f"Удалён временный файл: {full_path}")
                        except Exception as e:
                            logger.error(f"Ошибка при удалении файла {full_path}: {e}")
        except Exception as e:
            logger.error(f"Ошибка при очистке временных файлов: {e}")

    def cancel_download(self) -> None:
        if self.current_download:
            logger.info("Отмена текущей загрузки...")
            self.current_download.cancel()
            self.status_label.setText("Загрузка отменяется...")
            self.status_label.setStyleSheet("color: orange;")
            # При отмене загрузки сбрасываем индикатор прогресса
            self.progress_bar.setValue(0)
            # Восстанавливаем нормальный режим прогресс-бара, если он был в режиме ожидания
            self.progress_bar.setRange(0, 100)

    def on_mode_changed(self) -> None:
        is_video: bool = self.video_radio.isChecked()
        self.resolution_combo.setVisible(is_video)
        for i in range(self.resolution_layout.count()):
            widget = self.resolution_layout.itemAt(i).widget()
            if widget:
                widget.setVisible(is_video)

    def clear_queue(self) -> None:
        if not self.download_queue:
            return
        reply = QMessageBox.question(
            self,
            'Подтверждение',
            'Очистить очередь загрузок?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.download_queue.clear()
            self.update_queue_display()
            self.status_label.setText("Очередь очищена")

    def remove_selected(self) -> None:
        current_row: int = self.queue_list.currentRow()
        if current_row >= 0:
            del self.download_queue[current_row]
            self.update_queue_display()
            self.status_label.setText("Элемент удален из очереди")

    def is_valid_video_url(self, url: str) -> Tuple[bool, str]:
        """
        Проверяет валидность URL для поддерживаемых видеосервисов.
        Возвращает кортеж (валидность, сообщение об ошибке).
        """
        if not url:
            return False, "URL не может быть пустым"

        if not url.startswith(('http://', 'https://')):
            return False, "URL должен начинаться с http:// или https://"

        # Используем глобальный словарь URL_PATTERNS вместо создания его каждый раз
        for service, service_patterns in URL_PATTERNS.items():
            for pattern in service_patterns:
                if re.match(pattern, url):
                    logger.info(f"URL валиден для сервиса {service}: {url}")
                    return True, ""

        for service_name in URL_PATTERNS.keys():
            if service_name.lower() in url.lower():
                return False, f"Неверный формат URL для {service_name}. Проверьте правильность ссылки."

        return False, "Неподдерживаемый видеосервис или неверный формат URL"

    def set_controls_enabled(self, enabled: bool) -> None:
        """
        Включает или отключает элементы управления, чтобы предотвратить изменение очереди во время загрузки.
        """
        self.url_input.setEnabled(enabled)
        self.video_radio.setEnabled(enabled)
        self.audio_radio.setEnabled(enabled)
        self.resolution_combo.setEnabled(enabled)
        
    def show_about_dialog(self, event) -> None:
        """
        Показывает диалоговое окно с информацией о программе.
        """
        # Используем специальную функцию загрузки логотипа в формате PNG
        success, _, image_path = load_logo((120, 120))
        
        # Создаем текст с HTML-форматированием
        if success:
            # Если изображение найдено, включаем его в HTML с указанием пути
            about_text = (
                f"<div style='text-align: center;'><img src='{image_path}' width='120' height='120'/></div>"
                "<h2 style='text-align: center;'>Video Downloader v1.07</h2>"
                "<p>Приложение для скачивания видео и аудио с различных видеохостингов:</p>"
                "<ul>"
                "<li>YouTube</li>"
                "<li>VK</li>"
                "<li>RuTube</li>"
                "<li>Одноклассники</li>"
                "<li>Mail.ru</li>"
                "</ul>"
                "<p><b>Разработчик:</b> <a href='mailto:maks_k77@mail.ru'>maks_k77@mail.ru</a></p>"
                "<p><b>Поддержать проект:</b> Т-Банк 2200 7001 2147 7888</p>"
                "<p>© 2024-2025 Все права защищены</p>"
            )
        else:
            # Если изображение не найдено, показываем восклицательный знак
            about_text = (
                "<div style='text-align: center;'><span style='font-size: 80px; color: red;'>!</span></div>"
                "<h2 style='text-align: center;'>Video Downloader v1.07</h2>"
                "<p>Приложение для скачивания видео и аудио с различных видеохостингов:</p>"
                "<ul>"
                "<li>YouTube</li>"
                "<li>VK</li>"
                "<li>RuTube</li>"
                "<li>Одноклассники</li>"
                "<li>Mail.ru</li>"
                "</ul>"
                "<p><b>Разработчик:</b> <a href='mailto:maks_k77@mail.ru'>maks_k77@mail.ru</a></p>"
                "<p><b>Поддержать проект:</b> Т-Банк 2200 7001 2147 7888</p>"
                "<p>© 2024-2025 Все права защищены</p>"
            )
        
        # Создаем кастомное диалоговое окно с изображением
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("О программе")
        msg_box.setTextFormat(Qt.TextFormat.RichText)
        msg_box.setText(about_text)
        msg_box.setStandardButtons(QMessageBox.StandardButton.Ok)
        
        # Устанавливаем иконку, если нужно информационное изображение
        if not success:
            msg_box.setIcon(QMessageBox.Icon.Information)
        
        msg_box.exec()

# Проверка наличия необходимых компонентов
def check_ffmpeg() -> bool:
    """
    Проверяет наличие ffmpeg и ffprobe в системе.
    Возвращает True, если оба компонента найдены, иначе False.
    """
    ffmpeg_exists = shutil.which('ffmpeg') is not None
    ffprobe_exists = shutil.which('ffprobe') is not None
    
    logger.info(f"Проверка компонентов: ffmpeg: {ffmpeg_exists}, ffprobe: {ffprobe_exists}")
    return ffmpeg_exists and ffprobe_exists

def show_error_message(title: str, message: str) -> None:
    """
    Показывает диалоговое окно с сообщением об ошибке.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(title)
    box.setText(message)
    box.exec()
    sys.exit(1)

if __name__ == '__main__':
    # Проверка наличия ffmpeg и ffprobe перед запуском
    if not check_ffmpeg():
        error_message = (
            "Ошибка: Отсутствуют необходимые компоненты!\n\n"
            "Для работы программы требуются ffmpeg и ffprobe.\n\n"
            "Пожалуйста, установите ffmpeg и перезапустите программу.\n"
            "Инструкции по установке:\n"
            "- Windows: https://ffmpeg.org/download.html\n"
            "- Linux: sudo apt-get install ffmpeg\n"
            "- macOS: brew install ffmpeg"
        )
        show_error_message("Отсутствуют необходимые компоненты", error_message)
    
    app = QApplication(sys.argv)
    
    # Установка иконки для всего приложения с использованием load_logo
    success, pixmap, _ = load_logo((32, 32))
    if success:
        app_icon = QIcon(pixmap)
        app.setWindowIcon(app_icon)
        logger.info("Установлена иконка приложения для QApplication")
    
    window = VideoDownloaderUI()
    window.show()
    sys.exit(app.exec())
