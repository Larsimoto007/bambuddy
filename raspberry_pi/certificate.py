"""TLS certificate generation for the middleware.

Generates certificates for MQTT and FTP TLS connections:
- CA certificate (persistent, reused across restarts)
- Printer certificate (CN = serial number, signed by CA)

Standalone module - no external project dependencies.
"""

import logging
import socket
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

logger = logging.getLogger(__name__)

DEFAULT_SERIAL = "00M09A391800001"
CA_EXPIRY_THRESHOLD_DAYS = 30


def _get_local_ip() -> str:
    """Get the local IP address."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


class CertificateService:
    """Generate and manage TLS certificates for the middleware."""

    def __init__(self, cert_dir: Path, serial: str = DEFAULT_SERIAL):
        self.cert_dir = cert_dir
        self.serial = serial
        self.ca_cert_path = cert_dir / "bbl_ca.crt"
        self.ca_key_path = cert_dir / "bbl_ca.key"
        self.cert_path = cert_dir / "virtual_printer.crt"
        self.key_path = cert_dir / "virtual_printer.key"

    def ensure_certificates(self) -> tuple[Path, Path]:
        if self.cert_path.exists() and self.key_path.exists():
            logger.debug("Using existing certificates")
            return self.cert_path, self.key_path
        return self.generate_certificates()

    def _load_existing_ca(self):
        if not self.ca_cert_path.exists() or not self.ca_key_path.exists():
            return None

        try:
            ca_cert_pem = self.ca_cert_path.read_bytes()
            ca_cert = x509.load_pem_x509_certificate(ca_cert_pem)

            now = datetime.now(timezone.utc)
            days_remaining = (ca_cert.not_valid_after_utc - now).days
            if days_remaining < CA_EXPIRY_THRESHOLD_DAYS:
                logger.warning("CA certificate expires in %s days, regenerating", days_remaining)
                return None

            ca_key_pem = self.ca_key_path.read_bytes()
            ca_key = serialization.load_pem_private_key(ca_key_pem, password=None)

            logger.info("Using existing CA certificate (expires in %s days)", days_remaining)
            return ca_key, ca_cert

        except (OSError, ValueError) as e:
            logger.warning("Failed to load existing CA: %s", e)
            return None

    def _get_or_create_ca(self):
        existing = self._load_existing_ca()
        if existing:
            return existing

        ca_key, ca_cert = self._generate_ca_certificate()

        self.cert_dir.mkdir(parents=True, exist_ok=True)
        self.ca_key_path.write_bytes(
            ca_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        self.ca_key_path.chmod(0o600)
        self.ca_cert_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))

        logger.info("Saved new CA certificate")
        return ca_key, ca_cert

    def _generate_ca_certificate(self):
        logger.info("Generating new CA certificate...")

        ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Virtual Printer CA")])

        now = datetime.now(timezone.utc)

        ca_cert = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=7300))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(ca_key, hashes.SHA256())
        )

        return ca_key, ca_cert

    def generate_certificates(self, additional_ips: list[str] | None = None) -> tuple[Path, Path]:
        """Generate printer certificate signed by CA."""
        logger.info("Generating certificates for serial: %s...", self.serial)

        self.cert_dir.mkdir(parents=True, exist_ok=True)

        ca_key, ca_cert = self._get_or_create_ca()

        printer_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        printer_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, self.serial)])
        issuer = ca_cert.subject

        now = datetime.now(timezone.utc)
        local_ip = _get_local_ip()

        # Build SAN entries
        san_entries: list[x509.GeneralName] = [
            x509.DNSName("localhost"),
            x509.DNSName("bambuddy"),
            x509.DNSName(self.serial),
            x509.IPAddress(IPv4Address(local_ip)),
            x509.IPAddress(IPv4Address("127.0.0.1")),
        ]
        seen_ips = {local_ip, "127.0.0.1"}
        if additional_ips:
            for ip in additional_ips:
                if ip and ip not in seen_ips:
                    try:
                        san_entries.append(x509.IPAddress(IPv4Address(ip)))
                        seen_ips.add(ip)
                    except ValueError:
                        pass

        printer_cert = (
            x509.CertificateBuilder()
            .subject_name(printer_subject)
            .issuer_name(issuer)
            .public_key(printer_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
                critical=False,
            )
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=True,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(ca_key, hashes.SHA256())
        )

        self.key_path.write_bytes(
            printer_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        self.key_path.chmod(0o600)

        cert_chain = printer_cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(
            serialization.Encoding.PEM
        )
        self.cert_path.write_bytes(cert_chain)

        logger.info("Generated certificate chain at %s", self.cert_dir)
        return self.cert_path, self.key_path

    def delete_printer_certificate(self) -> None:
        for path in [self.cert_path, self.key_path]:
            if path.exists():
                path.unlink()
