use std::fmt::{Display, Formatter, Write as FmtWrite};
use std::fs;
use std::io::{self, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[derive(Debug)]
pub struct FirecrackerApiError {
    pub status_line: String,
    pub response: String,
}

#[derive(Debug)]
pub enum FirecrackerError {
    Io(io::Error),
    Api(FirecrackerApiError),
    Timeout(String),
    ProcessExited,
    ProcessAlreadyRunning,
    ProcessNotRunning,
}

impl Display for FirecrackerError {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        match self {
            FirecrackerError::Io(e) => write!(f, "{e}"),
            FirecrackerError::Api(e) => write!(f, "{}\n{}", e.status_line, e.response),
            FirecrackerError::Timeout(msg) => write!(f, "{msg}"),
            FirecrackerError::ProcessExited => write!(f, "Firecracker exited before socket became ready"),
            FirecrackerError::ProcessAlreadyRunning => write!(f, "Firecracker process is already running"),
            FirecrackerError::ProcessNotRunning => write!(f, "Firecracker process is not running"),
        }
    }
}

impl std::error::Error for FirecrackerError {}

impl From<io::Error> for FirecrackerError {
    fn from(value: io::Error) -> Self {
        FirecrackerError::Io(value)
    }
}

type Result<T> = std::result::Result<T, FirecrackerError>;

pub struct FirecrackerVM {
    firecracker_bin: String,
    socket_path: PathBuf,
    kernel_image_path: PathBuf,
    boot_args: String,
    startup_timeout: Duration,
    request_timeout: Duration,
    child: Option<Child>,
}

impl FirecrackerVM {
    pub fn new(
        firecracker_bin: impl Into<String>,
        socket_path: impl Into<PathBuf>,
        kernel_image_path: impl Into<PathBuf>,
    ) -> Self {
        Self {
            firecracker_bin: firecracker_bin.into(),
            socket_path: socket_path.into(),
            kernel_image_path: kernel_image_path.into(),
            boot_args: "console=ttyS0 reboot=k panic=1 pci=off".to_string(),
            startup_timeout: Duration::from_secs(5),
            request_timeout: Duration::from_secs(3),
            child: None,
        }
    }

    pub fn set_boot_args(&mut self, boot_args: impl Into<String>) {
        self.boot_args = boot_args.into();
    }

    pub fn set_startup_timeout(&mut self, timeout: Duration) {
        self.startup_timeout = timeout;
    }

    pub fn set_request_timeout(&mut self, timeout: Duration) {
        self.request_timeout = timeout;
    }

    pub fn create(&mut self) -> Result<()> {
        if let Some(child) = self.child.as_mut() {
            if child.try_wait()?.is_none() {
                return Err(FirecrackerError::ProcessAlreadyRunning);
            }
        }
        if self.socket_path.exists() {
            fs::remove_file(&self.socket_path)?;
        }
        let child = Command::new(&self.firecracker_bin)
            .arg("--api-sock")
            .arg(&self.socket_path)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()?;
        self.child = Some(child);
        self.wait_for_socket()?;
        let payload = format!(
            "{{\"kernel_image_path\":\"{}\",\"boot_args\":\"{}\"}}",
            json_escape(self.kernel_image_path.to_string_lossy().as_ref()),
            json_escape(&self.boot_args)
        );
        self.put("/boot-source", &payload)?;
        Ok(())
    }

    pub fn configure_cpu_memory(&self, vcpu_count: u32, mem_size_mib: u32) -> Result<()> {
        self.configure_cpu_memory_advanced(vcpu_count, mem_size_mib, false, false)
    }

    pub fn configure_cpu_memory_advanced(
        &self,
        vcpu_count: u32,
        mem_size_mib: u32,
        smt: bool,
        track_dirty_pages: bool,
    ) -> Result<()> {
        let payload = format!(
            "{{\"vcpu_count\":{vcpu_count},\"mem_size_mib\":{mem_size_mib},\"smt\":{smt},\"track_dirty_pages\":{track_dirty_pages}}}"
        );
        self.put("/machine-config", &payload)?;
        Ok(())
    }

    pub fn attach_rootfs(&self, path_on_host: &str) -> Result<()> {
        self.attach_rootfs_with_mode(path_on_host, false)
    }

    pub fn attach_rootfs_with_mode(&self, path_on_host: &str, read_only: bool) -> Result<()> {
        let payload = format!(
            "{{\"drive_id\":\"rootfs\",\"path_on_host\":\"{}\",\"is_root_device\":true,\"is_read_only\":{read_only}}}",
            json_escape(path_on_host)
        );
        self.put("/drives/rootfs", &payload)?;
        Ok(())
    }

    pub fn attach_job_disk(&self, path_on_host: &str) -> Result<()> {
        self.attach_job_disk_with_id("job", path_on_host, false)
    }

    pub fn attach_job_disk_with_id(&self, drive_id: &str, path_on_host: &str, read_only: bool) -> Result<()> {
        let payload = format!(
            "{{\"drive_id\":\"{}\",\"path_on_host\":\"{}\",\"is_root_device\":false,\"is_read_only\":{read_only}}}",
            json_escape(drive_id),
            json_escape(path_on_host)
        );
        let path = format!("/drives/{}", drive_id);
        self.put(&path, &payload)?;
        Ok(())
    }

    pub fn attach_network(&self, host_dev_name: &str, guest_mac: &str) -> Result<()> {
        self.attach_network_with_iface("eth0", host_dev_name, guest_mac)
    }

    pub fn attach_network_with_iface(&self, iface_id: &str, host_dev_name: &str, guest_mac: &str) -> Result<()> {
        let payload = format!(
            "{{\"iface_id\":\"{}\",\"host_dev_name\":\"{}\",\"guest_mac\":\"{}\"}}",
            json_escape(iface_id),
            json_escape(host_dev_name),
            json_escape(guest_mac)
        );
        let path = format!("/network-interfaces/{}", iface_id);
        self.put(&path, &payload)?;
        Ok(())
    }

    pub fn start(&self) -> Result<()> {
        self.put("/actions", "{\"action_type\":\"InstanceStart\"}")?;
        Ok(())
    }

    pub fn wait_for_exit(&mut self, timeout: Option<Duration>) -> Result<Option<ExitStatus>> {
        let child = self.child.as_mut().ok_or(FirecrackerError::ProcessNotRunning)?;
        match timeout {
            None => Ok(Some(child.wait()?)),
            Some(limit) => {
                let deadline = Instant::now() + limit;
                loop {
                    if let Some(status) = child.try_wait()? {
                        return Ok(Some(status));
                    }
                    if Instant::now() >= deadline {
                        return Ok(None);
                    }
                    thread::sleep(Duration::from_millis(50));
                }
            }
        }
    }

    pub fn kill(&mut self) -> Result<()> {
        if let Some(child) = self.child.as_mut() {
            if child.try_wait()?.is_none() {
                child.kill()?;
                let _ = child.wait();
            }
        }
        Ok(())
    }

    pub fn cleanup(&mut self) -> Result<()> {
        let _ = self.kill();
        self.child = None;
        if self.socket_path.exists() {
            fs::remove_file(&self.socket_path)?;
        }
        Ok(())
    }

    fn wait_for_socket(&mut self) -> Result<()> {
        let deadline = Instant::now() + self.startup_timeout;
        while Instant::now() < deadline {
            if let Some(child) = self.child.as_mut() {
                if child.try_wait()?.is_some() {
                    return Err(FirecrackerError::ProcessExited);
                }
            }
            if self.socket_path.exists() {
                match self.request("GET", "/", None) {
                    Ok(_) => return Ok(()),
                    Err(FirecrackerError::Api(_)) => return Ok(()),
                    Err(FirecrackerError::Io(e))
                        if matches!(
                            e.kind(),
                            io::ErrorKind::ConnectionRefused
                                | io::ErrorKind::NotFound
                                | io::ErrorKind::TimedOut
                        ) => {}
                    Err(e) => return Err(e),
                }
            }
            thread::sleep(Duration::from_millis(50));
        }
        Err(FirecrackerError::Timeout(format!(
            "Timed out waiting for Firecracker socket: {}",
            self.socket_path.display()
        )))
    }

    fn put(&self, path: &str, payload: &str) -> Result<Vec<u8>> {
        self.request("PUT", path, Some(payload))
    }

    fn request(&self, method: &str, path: &str, payload: Option<&str>) -> Result<Vec<u8>> {
        let body = payload.unwrap_or("").as_bytes();
        let request = format!(
            "{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            body.len()
        );
        let mut sock = UnixStream::connect(&self.socket_path)?;
        sock.set_read_timeout(Some(self.request_timeout))?;
        sock.set_write_timeout(Some(self.request_timeout))?;
        sock.write_all(request.as_bytes())?;
        if !body.is_empty() {
            sock.write_all(body)?;
        }
        let mut response = Vec::new();
        sock.read_to_end(&mut response)?;
        let status_line = {
            let raw = String::from_utf8_lossy(&response);
            match raw.find("\r\n") {
                Some(i) => raw[..i].to_string(),
                None => raw.into_owned(),
            }
        };
        if !status_line.starts_with("HTTP/1.1 2") {
            return Err(FirecrackerError::Api(FirecrackerApiError {
                status_line,
                response: String::from_utf8_lossy(&response).into_owned(),
            }));
        }
        Ok(response)
    }
}

fn json_escape(input: &str) -> String {
    let mut out = String::with_capacity(input.len() + 8);
    for c in input.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c <= '\u{1F}' => {
                let _ = write!(&mut out, "\\u{:04x}", c as u32);
            }
            c => out.push(c),
        }
    }
    out
}
