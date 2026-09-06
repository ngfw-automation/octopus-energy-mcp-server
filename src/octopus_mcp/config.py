from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven configuration. See ``.env.example`` for the full set."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Required -------------------------------------------------------
    octopus_api_key: str
    octopus_account_number: str

    @field_validator("octopus_api_key", "octopus_account_number")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        if value is not None and not value.strip():
            raise ValueError("must be a non-empty string")
        return value

    # --- Optional meter pinning ----------------------------------------
    octopus_electricity_mpan: str | None = None
    octopus_electricity_serial: str | None = None
    octopus_gas_mprn: str | None = None
    octopus_gas_serial: str | None = None
    octopus_export_mpan: str | None = None

    # --- Optional payment method ----------------------------------------
    # Variable tariffs price by payment method (direct debit is discounted).
    # Sets the default rate picked by get_unit_rates and cost calculations.
    default_payment_method: str | None = None

    @field_validator("default_payment_method")
    @classmethod
    def _norm_payment_method(cls, value: str | None) -> str | None:
        if value is None:
            return None
        v = value.strip().upper()
        if not v:
            return None
        if v not in ("DIRECT_DEBIT", "NON_DIRECT_DEBIT"):
            raise ValueError(
                "must be DIRECT_DEBIT or NON_DIRECT_DEBIT (or empty/unset)"
            )
        return v

    # --- Gas units ------------------------------------------------------
    # SMETS1 gas meters report kWh over the REST API; SMETS2 meters report
    # m3. The /accounts/ payload carries no field that distinguishes them, so
    # the unit cannot be detected reliably -- pin it here ("kwh" or "m3").
    # Unset means "assume kWh", the documented default, with a warning on
    # every gas response.
    octopus_gas_units: str | None = None

    @field_validator("octopus_gas_units", mode="before")
    @classmethod
    def _norm_gas_units(cls, value: object) -> str | None:
        if value is None:
            return None
        v = str(value).strip().lower().replace("\u00b3", "3")
        if not v:
            return None
        if v in ("kwh", "kw/h"):
            return "kwh"
        if v in ("m3", "m^3", "cubic_metres", "cubic_meters"):
            return "m3"
        raise ValueError("must be kwh or m3 (or empty/unset)")

    # Calorific value in MJ/m3 used to convert m3 -> kWh
    # (kWh = m3 x CV x 1.02264 / 3.6). It is printed on the gas bill and
    # varies by region and period; 39.5 is a typical UK value (~11.22 kWh/m3).
    gas_calorific_value: float = 39.5

    @field_validator("gas_calorific_value", mode="before")
    @classmethod
    def _cv_sane(cls, value: object) -> float:
        if value is None or (isinstance(value, str) and not value.strip()):
            return 39.5
        v = float(value)
        if not 30.0 <= v <= 45.0:
            raise ValueError("must be a calorific value in MJ/m3, roughly 30-45")
        return v

    # --- Standing charge override (VAT-inclusive pence/day) --------------
    # Only used when the API publishes no standing charge for the tariff.
    # Give the figure exactly as the dashboard shows it -- VAT included --
    # and the server converts it back to ex-VAT internally.
    standing_charge_electricity: float | None = None
    standing_charge_gas: float | None = None

    @field_validator("standing_charge_electricity", "standing_charge_gas", mode="before")
    @classmethod
    def _sc_non_negative(cls, value: object) -> float | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None  # compose passes ${VAR:-} as "" when unset
        v = float(value)
        if v < 0:
            raise ValueError("must be >= 0 (pence per day, VAT inclusive)")
        return v

    # --- Cache TTLs (seconds) ------------------------------------------
    cache_ttl_products: int = 86400
    cache_ttl_rates: int = 86400
    cache_ttl_consumption: int = 1800
    cache_ttl_account: int = 3600

    # --- Downsample caps -------------------------------------------------
    max_rows_returned: int = 500

    # Widest range include_raw will answer for. One row per half hour is
    # ~1,500 rows a month, which is tens of thousands of tokens in a single
    # tool result.
    max_raw_days: int = 2


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings`, reading the environment / ``.env`` once."""
    return Settings()
