CREATE TABLE IF NOT EXISTS rollouts (
    id TEXT PRIMARY KEY,

    name TEXT NOT NULL,
    runtime TEXT NOT NULL DEFAULT 'Dockerfile'
        CHECK (runtime IN ('Dockerfile')),
    dockerfile_path TEXT NOT NULL,

    rollout_dir TEXT NOT NULL,
    image_path TEXT NOT NULL,

    delete_on_success INTEGER NOT NULL DEFAULT 0
        CHECK (delete_on_success IN (0, 1)),

    resolved_run_command_json TEXT,
    runtime_image_json TEXT,
    vm_config_json TEXT,

    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN (
            'created',
            'scheduled',
            'running',
            'passed',
            'failed',
            'retry_pending',
            'retrying',
            'exhausted',
            'cancelled'
        )),
    priority INTEGER NOT NULL DEFAULT 100,

    retry_count INTEGER NOT NULL DEFAULT 0
        CHECK (retry_count >= 0),
    max_retries INTEGER NOT NULL DEFAULT 3
        CHECK (max_retries >= 0),

    active_worker_id TEXT,
    last_worker_id TEXT,

    created_at TEXT NOT NULL,
    scheduled_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rollouts_status_priority
ON rollouts(status, priority DESC, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_rollouts_active_worker
ON rollouts(active_worker_id);


CREATE TABLE IF NOT EXISTS runtime_images (
    id TEXT PRIMARY KEY,

    rollout_id TEXT NOT NULL,
    path TEXT NOT NULL,
    metadata_path TEXT,

    size_bytes INTEGER
        CHECK (size_bytes IS NULL OR size_bytes >= 0),
    docker_image_id TEXT,
    docker_image_tag TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,

    FOREIGN KEY (rollout_id) REFERENCES rollouts(id) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,

    rollout_id TEXT NOT NULL,
    reservation_id TEXT,

    attempt INTEGER NOT NULL DEFAULT 1
        CHECK (attempt >= 1),
    retry_of TEXT,

    vcpu INTEGER NOT NULL
        CHECK (vcpu > 0),
    memory TEXT NOT NULL,
    memory_bytes INTEGER NOT NULL
        CHECK (memory_bytes > 0),
    disk TEXT NOT NULL,
    disk_bytes INTEGER NOT NULL
        CHECK (disk_bytes > 0),
    timeout_seconds REAL NOT NULL
        CHECK (timeout_seconds > 0),
    network INTEGER NOT NULL DEFAULT 1
        CHECK (network IN (0, 1)),
    env_json TEXT,

    pid INTEGER
        CHECK (pid IS NULL OR pid > 0),

    worker_dir TEXT NOT NULL,
    rootfs_path TEXT,
    execution_disk_path TEXT,
    firecracker_sock_path TEXT,
    firecracker_log_path TEXT,
    result_path TEXT,
    failure_path TEXT,

    status TEXT NOT NULL DEFAULT 'reserved'
        CHECK (status IN (
            'reserved',
            'starting',
            'running',
            'passed',
            'failed',
            'timeout',
            'lost'
        )),

    exit_code INTEGER,
    failure_json TEXT DEFAULT '{}',
    failure_phase TEXT,

    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    updated_at TEXT NOT NULL,

    FOREIGN KEY (rollout_id) REFERENCES rollouts(id) ON DELETE CASCADE,
    FOREIGN KEY (retry_of) REFERENCES workers(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_workers_rollout
ON workers(rollout_id);

CREATE INDEX IF NOT EXISTS idx_workers_status
ON workers(status);

CREATE INDEX IF NOT EXISTS idx_workers_pid
ON workers(pid);


CREATE TABLE IF NOT EXISTS reservations (
    id TEXT PRIMARY KEY,

    worker_id TEXT NOT NULL,
    rollout_id TEXT NOT NULL,

    pid INTEGER
        CHECK (pid IS NULL OR pid > 0),

    vcpu INTEGER NOT NULL
        CHECK (vcpu > 0),
    memory TEXT NOT NULL,
    memory_bytes INTEGER NOT NULL
        CHECK (memory_bytes > 0),
    disk TEXT NOT NULL,
    disk_bytes INTEGER NOT NULL
        CHECK (disk_bytes > 0),

    status TEXT NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved', 'starting', 'running', 'released', 'lost')),

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    released_at TEXT,
    last_heartbeat_at TEXT,

    FOREIGN KEY (worker_id) REFERENCES workers(id) ON DELETE CASCADE,
    FOREIGN KEY (rollout_id) REFERENCES rollouts(id) ON DELETE CASCADE
);


CREATE INDEX IF NOT EXISTS idx_reservations_active
ON reservations(status);


CREATE TABLE IF NOT EXISTS machine_policy (
    id INTEGER PRIMARY KEY CHECK (id = 1),

    host_reserved_memory TEXT NOT NULL DEFAULT '2G',
    host_reserved_memory_bytes INTEGER NOT NULL
        CHECK (host_reserved_memory_bytes >= 0),

    host_reserved_disk TEXT NOT NULL DEFAULT '20G',
    host_reserved_disk_bytes INTEGER NOT NULL
        CHECK (host_reserved_disk_bytes >= 0),

    max_memory_percent INTEGER NOT NULL DEFAULT 80
        CHECK (max_memory_percent >= 0 AND max_memory_percent <= 100),
    max_disk_percent INTEGER NOT NULL DEFAULT 80
        CHECK (max_disk_percent >= 0 AND max_disk_percent <= 100),
    max_concurrent_vms INTEGER NOT NULL DEFAULT 4
        CHECK (max_concurrent_vms > 0),

    vm_memory_overhead TEXT NOT NULL DEFAULT '256M',
    vm_memory_overhead_bytes INTEGER NOT NULL
        CHECK (vm_memory_overhead_bytes >= 0),

    vm_disk_overhead TEXT NOT NULL DEFAULT '2G',
    vm_disk_overhead_bytes INTEGER NOT NULL
        CHECK (vm_disk_overhead_bytes >= 0),

    poll_interval REAL NOT NULL DEFAULT 5.0
        CHECK (poll_interval > 0),
    cooldown_after_vm REAL NOT NULL DEFAULT 5.0
        CHECK (cooldown_after_vm >= 0),

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,

    event_type TEXT NOT NULL,
    message TEXT,

    data_json TEXT,

    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_entity
ON events(entity_type, entity_id, created_at);


CREATE TABLE IF NOT EXISTS network_leases (
    id TEXT PRIMARY KEY,

    worker_id TEXT NOT NULL,
    rollout_id TEXT,

    network_name TEXT NOT NULL,
    namespace_name TEXT NOT NULL,
    namespace_path TEXT NOT NULL,

    ifname TEXT NOT NULL DEFAULT 'veth0',
    tap_name TEXT NOT NULL DEFAULT 'tap0',

    guest_ip TEXT,
    guest_cidr TEXT,
    gateway TEXT,
    dns_json TEXT,
    result_json TEXT,

    status TEXT NOT NULL DEFAULT 'created'
        CHECK (status IN ('created', 'active', 'released', 'failed')),

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    released_at TEXT,

    FOREIGN KEY (worker_id) REFERENCES workers(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_network_leases_worker
ON network_leases(worker_id);

CREATE INDEX IF NOT EXISTS idx_network_leases_status
ON network_leases(status);
