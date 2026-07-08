from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# --- Config models (parsed from arfea.yml) ---


class HealthcheckConfig(BaseModel):
    test: list[str]
    interval: int = 30
    timeout: int = 10
    retries: int = 3
    start_period: int = 120


class DependsOnCondition(BaseModel):
    condition: str = "service_started"


class ServiceDefinition(BaseModel):
    enabled: bool = False
    core: bool = False
    image: str
    container_name: str
    restart_policy: str = "unless-stopped"
    network_mode: Optional[str] = None
    ports: list[str] = Field(default_factory=list)
    volumes: list[str] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    devices: list[str] = Field(default_factory=list)
    cap_add: list[str] = Field(default_factory=list)
    privileged: bool = False
    dns: list[str] = Field(default_factory=list)
    group_add: list[str] = Field(default_factory=list)
    depends_on: dict[str, DependsOnCondition] = Field(default_factory=dict)
    healthcheck: Optional[HealthcheckConfig] = None
    log_max_size: str = "10m"
    env_file: Optional[str] = None


class DependencyRule(BaseModel):
    when_any_enabled: list[str]
    then_enable: str


class NetworkConfig(BaseModel):
    name: str = "domotica"
    subnet: str = "172.11.0.0/24"
    gateway: str = "172.11.0.1"


class BackupConfig(BaseModel):
    webdav_url: str = ""
    webdav_user: str = ""
    webdav_password: str = ""
    exclude_paths: list[str] = Field(default_factory=list)


class ControllerSettings(BaseModel):
    port: int = 8888
    data_path: str = "/opt/docker_store"
    log_level: str = "info"
    api_key: str = ""
    update_url: str = ""
    # URL del manifest delle versioni certificate (releases.json). Separato da
    # update_url, che riguarda solo l'OTA del codice del controller.
    releases_url: str = ""
    # Release certificata a cui questa centralina è allineata (es. "2026.04").
    # Vuoto = mai allineata: l'apply parte dalla più vecchia del manifest.
    release: str = ""


class LinphoneConfig(BaseModel):
    """Configurazione chiamate di emergenza via linphone (dentro container OpenHAB)."""
    enabled: bool = False
    sip_host: str = ""
    sip_username: str = ""
    sip_password: str = ""
    emergency_number: str = ""
    message: str = "Allarme dal sistema domotico ARFEA."
    call_timeout: int = 30   # secondi di attesa risposta
    repeat: int = 2          # quante volte ripetere il messaggio durante la chiamata


class LinphoneConfigUpdate(BaseModel):
    """Body parziale per aggiornare la configurazione linphone (campi opzionali)."""
    enabled: Optional[bool] = None
    sip_host: Optional[str] = None
    sip_username: Optional[str] = None
    sip_password: Optional[str] = None
    emergency_number: Optional[str] = None
    message: Optional[str] = None
    call_timeout: Optional[int] = None
    repeat: Optional[int] = None


class ArfeaConfig(BaseModel):
    controller: ControllerSettings = Field(default_factory=ControllerSettings)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    linphone: LinphoneConfig = Field(default_factory=LinphoneConfig)
    dependencies: list[DependencyRule] = Field(default_factory=list)
    services: dict[str, ServiceDefinition] = Field(default_factory=dict)


# --- API response models ---


class ContainerState(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    NOT_CREATED = "not_created"
    RESTARTING = "restarting"
    PAUSED = "paused"
    DEAD = "dead"


class HealthState(str, Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    STARTING = "starting"
    NONE = "none"


class ServiceStatus(BaseModel):
    name: str
    container_name: str
    enabled: bool
    effectively_enabled: bool
    core: bool
    state: ContainerState
    health: HealthState = HealthState.NONE
    image: str
    ports: list[str] = Field(default_factory=list)


class OperationResponse(BaseModel):
    success: bool
    message: str
    details: Optional[dict] = None


class BackupState(str, Enum):
    IDLE = "idle"
    STOPPING_CONTAINERS = "stopping_containers"
    CREATING_ARCHIVE = "creating_archive"
    UPLOADING = "uploading"
    RESTARTING_CONTAINERS = "restarting_containers"
    COMPLETED = "completed"
    FAILED = "failed"


class BackupStatus(BaseModel):
    state: BackupState = BackupState.IDLE
    message: str = ""
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class NetworkInfo(BaseModel):
    lan_ip: str = "N/A"
    vpn_ip: str = "N/A"
    external_ip: str = "N/A"


# --- Aggiornamento versioni immagini (OTA "release certificate") ---


class ReleaseUpdateState(str, Enum):
    IDLE = "idle"
    BACKUP = "backup"
    MIGRATING_PRE = "migrating_pre"
    PULLING = "pulling"
    RECREATING = "recreating"
    WAITING_HEALTHY = "waiting_healthy"
    MIGRATING_POST = "migrating_post"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class ReleaseUpdateStatus(BaseModel):
    state: ReleaseUpdateState = ReleaseUpdateState.IDLE
    message: str = ""
    current_release: str = ""
    target_release: str = ""
    step: str = ""               # es. "2/3"
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class ServiceUpdateInfo(BaseModel):
    """Aggiornamento immagine disponibile per un singolo servizio."""
    name: str                    # nome servizio in arfea.yml (es. "openhab")
    current_image: str
    target_image: str


class ReleaseCheckResult(BaseModel):
    update_available: bool = False
    current_release: str = ""
    latest_release: str = ""
    path: list[str] = Field(default_factory=list)   # release da attraversare in ordine
    services: list[ServiceUpdateInfo] = Field(default_factory=list)  # diff per-servizio verso latest
    notes: str = ""
    error: str = ""


class SystemInfo(BaseModel):
    hostname: str = ""
    version: str = "1.4.0"
    uptime: str = ""
