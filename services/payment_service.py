"""
Vista-Energy — Payment Service (PayPal Subscriptions)

Verwaltet PayPal-Abonnements und Einmalzahlungen.

Ablauf Abo (monatlich/jaehrlich):
  1. Kunde klickt "Abo starten" im Dashboard
  2. PayPal Checkout-Dialog oeffnet sich (clientseitig via PayPal JS SDK)
  3. Kunde autorisiert Abo bei PayPal
  4. PayPal sendet subscription_id an unseren Server
  5. Server erstellt Lizenzschluessel + bindet an Hardware-ID
  6. PayPal zieht monatlich/jaehrlich automatisch ab
  7. PayPal sendet Webhook → Server verlaengert Lizenz automatisch
  8. Bei Kuendigung: PayPal Webhook → Lizenz laeuft zum Ende der Periode aus

Ablauf Einmalkauf:
  1. Kunde klickt "Einmalkauf" im Dashboard
  2. PayPal Checkout → Order wird erstellt
  3. Nach Zahlung: Lizenzschluessel wird generiert (12 Monate Laufzeit)

PayPal-Konfiguration (config.yaml):
  payment:
    paypal_client_id: YOUR_CLIENT_ID
    paypal_secret: YOUR_SECRET
    paypal_mode: sandbox | live
    monthly_plan_id: P-XXXXX       # PayPal Subscription Plan ID
    yearly_plan_id: P-YYYYY
    price_monthly: 4.99
    price_yearly: 49.99
    price_lifetime: 149.99
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
import string
from datetime import datetime, timedelta
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)


class PaymentService:
    """PayPal Abo- und Zahlungsverwaltung."""

    PAYPAL_SANDBOX_URL = "https://api-m.sandbox.paypal.com"
    PAYPAL_LIVE_URL = "https://api-m.paypal.com"

    PLANS = {
        'monthly': {
            'name': 'Vista-Energy Monatlich',
            'price': 4.99,
            'currency': 'EUR',
            'interval': 'MONTH',
            'license_days': 35,
        },
        'yearly': {
            'name': 'Vista-Energy Jaehrlich',
            'price': 49.99,
            'currency': 'EUR',
            'interval': 'YEAR',
            'license_days': 370,
        },
        'lifetime': {
            'name': 'Vista-Energy Lifetime',
            'price': 149.99,
            'currency': 'EUR',
            'interval': None,
            'license_days': 36500,
        },
    }

    def __init__(self, config: dict = None):
        cfg = config or {}
        payment_cfg = cfg.get('payment', {})
        self.client_id = payment_cfg.get(
            'paypal_client_id', os.getenv('PAYPAL_CLIENT_ID', '')
        )
        self.client_secret = payment_cfg.get(
            'paypal_secret', os.getenv('PAYPAL_SECRET', '')
        )
        self.mode = payment_cfg.get(
            'paypal_mode', os.getenv('PAYPAL_MODE', 'sandbox')
        )
        self.monthly_plan_id = payment_cfg.get('monthly_plan_id', '')
        self.yearly_plan_id = payment_cfg.get('yearly_plan_id', '')
        self.webhook_id = payment_cfg.get(
            'paypal_webhook_id', os.getenv('PAYPAL_WEBHOOK_ID', '')
        )

        for plan_key in self.PLANS:
            cfg_price = payment_cfg.get(f'price_{plan_key}')
            if cfg_price is not None:
                self.PLANS[plan_key]['price'] = float(cfg_price)

        self._access_token = None
        self._token_expires = None

    @property
    def api_base(self) -> str:
        if self.mode == 'live':
            return self.PAYPAL_LIVE_URL
        return self.PAYPAL_SANDBOX_URL

    @property
    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    # ------------------------------------------------------------------
    # PayPal OAuth Token
    # ------------------------------------------------------------------

    def _get_access_token(self) -> Optional[str]:
        if (self._access_token and self._token_expires
                and datetime.now() < self._token_expires):
            return self._access_token

        try:
            resp = requests.post(
                f"{self.api_base}/v1/oauth2/token",
                auth=(self.client_id, self.client_secret),
                data={'grant_type': 'client_credentials'},
                headers={'Accept': 'application/json'},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            self._access_token = data['access_token']
            self._token_expires = datetime.now() + timedelta(
                seconds=data.get('expires_in', 3600) - 60
            )
            return self._access_token
        except Exception as e:
            logger.error(f"PayPal OAuth fehlgeschlagen: {e}")
            return None

    def _api_headers(self) -> dict:
        token = self._get_access_token()
        return {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }

    # ------------------------------------------------------------------
    # Abo erstellen (Subscription)
    # ------------------------------------------------------------------

    def create_subscription(self, plan: str, hardware_id: str,
                            return_url: str, cancel_url: str) -> Dict:
        """PayPal-Abo starten.

        Args:
            plan: 'monthly' oder 'yearly'
            hardware_id: Hardware-ID des Kundengeraets
            return_url: Redirect nach Erfolg
            cancel_url: Redirect bei Abbruch

        Returns:
            {'success': bool, 'approval_url': str, 'subscription_id': str}
        """
        if plan not in ('monthly', 'yearly'):
            return {'success': False, 'message': 'Ungueltiger Plan'}

        plan_id = (self.monthly_plan_id if plan == 'monthly'
                   else self.yearly_plan_id)
        if not plan_id:
            return {
                'success': False,
                'message': f'PayPal Plan-ID fuer {plan} nicht konfiguriert'
            }

        try:
            body = {
                'plan_id': plan_id,
                'custom_id': hardware_id,
                'application_context': {
                    'brand_name': 'Vista-Energy',
                    'locale': 'de-DE',
                    'shipping_preference': 'NO_SHIPPING',
                    'user_action': 'SUBSCRIBE_NOW',
                    'return_url': return_url,
                    'cancel_url': cancel_url,
                },
            }

            resp = requests.post(
                f"{self.api_base}/v1/billing/subscriptions",
                headers=self._api_headers(),
                json=body,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            approval_url = next(
                (l['href'] for l in data.get('links', [])
                 if l['rel'] == 'approve'),
                None
            )

            return {
                'success': True,
                'subscription_id': data['id'],
                'approval_url': approval_url,
            }
        except Exception as e:
            logger.error(f"PayPal Subscription erstellen fehlgeschlagen: {e}")
            return {'success': False, 'message': str(e)}

    # ------------------------------------------------------------------
    # Einmalkauf (Order)
    # ------------------------------------------------------------------

    def create_order(self, plan: str, hardware_id: str) -> Dict:
        """PayPal-Order fuer Einmalkauf erstellen.

        Args:
            plan: 'lifetime' (oder 'yearly' als Einmalkauf)
            hardware_id: Hardware-ID

        Returns:
            {'success': bool, 'order_id': str}
        """
        plan_info = self.PLANS.get(plan)
        if not plan_info:
            return {'success': False, 'message': 'Ungueltiger Plan'}

        try:
            body = {
                'intent': 'CAPTURE',
                'purchase_units': [{
                    'amount': {
                        'currency_code': plan_info['currency'],
                        'value': f"{plan_info['price']:.2f}",
                    },
                    'description': plan_info['name'],
                    'custom_id': hardware_id,
                }],
                'application_context': {
                    'brand_name': 'Vista-Energy',
                    'locale': 'de-DE',
                    'shipping_preference': 'NO_SHIPPING',
                },
            }

            resp = requests.post(
                f"{self.api_base}/v2/checkout/orders",
                headers=self._api_headers(),
                json=body,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            return {
                'success': True,
                'order_id': data['id'],
            }
        except Exception as e:
            logger.error(f"PayPal Order erstellen fehlgeschlagen: {e}")
            return {'success': False, 'message': str(e)}

    def capture_order(self, order_id: str) -> Dict:
        """PayPal-Order abschliessen (nach Kundengenehmigung).

        Returns:
            {'success': bool, 'hardware_id': str, 'plan': str}
        """
        try:
            resp = requests.post(
                f"{self.api_base}/v2/checkout/orders/{order_id}/capture",
                headers=self._api_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get('status') == 'COMPLETED':
                capture = data['purchase_units'][0]['payments']['captures'][0]
                hardware_id = data['purchase_units'][0].get('custom_id', '')
                return {
                    'success': True,
                    'hardware_id': hardware_id,
                    'transaction_id': capture['id'],
                    'amount': capture['amount']['value'],
                }
            return {'success': False, 'message': 'Zahlung nicht abgeschlossen'}
        except Exception as e:
            logger.error(f"PayPal Capture fehlgeschlagen: {e}")
            return {'success': False, 'message': str(e)}

    # ------------------------------------------------------------------
    # Abo-Status pruefen
    # ------------------------------------------------------------------

    def get_subscription_status(self, subscription_id: str) -> Dict:
        """Status eines PayPal-Abos abfragen."""
        try:
            resp = requests.get(
                f"{self.api_base}/v1/billing/subscriptions/{subscription_id}",
                headers=self._api_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            return {
                'status': data.get('status'),
                'plan_id': data.get('plan_id'),
                'custom_id': data.get('custom_id'),
                'next_billing': data.get('billing_info', {}).get(
                    'next_billing_time'
                ),
                'payer_email': data.get('subscriber', {}).get(
                    'email_address'
                ),
            }
        except Exception as e:
            logger.error(f"PayPal Subscription-Status Fehler: {e}")
            return {'status': 'error', 'message': str(e)}

    # ------------------------------------------------------------------
    # Webhook-Verarbeitung
    # ------------------------------------------------------------------

    def verify_webhook(self, headers: dict, body: bytes) -> bool:
        """PayPal Webhook-Signatur verifizieren."""
        if not self.webhook_id:
            logger.warning("PayPal Webhook-ID nicht konfiguriert, "
                           "Signatur-Check uebersprungen")
            return True

        try:
            verify_body = {
                'auth_algo': headers.get('PAYPAL-AUTH-ALGO', ''),
                'cert_url': headers.get('PAYPAL-CERT-URL', ''),
                'transmission_id': headers.get('PAYPAL-TRANSMISSION-ID', ''),
                'transmission_sig': headers.get('PAYPAL-TRANSMISSION-SIG', ''),
                'transmission_time': headers.get('PAYPAL-TRANSMISSION-TIME', ''),
                'webhook_id': self.webhook_id,
                'webhook_event': json.loads(body),
            }

            resp = requests.post(
                f"{self.api_base}/v1/notifications/verify-webhook-signature",
                headers=self._api_headers(),
                json=verify_body,
                timeout=15,
            )
            resp.raise_for_status()
            result = resp.json()
            return result.get('verification_status') == 'SUCCESS'
        except Exception as e:
            logger.error(f"Webhook-Verifizierung fehlgeschlagen: {e}")
            return False

    def process_webhook(self, event_type: str, resource: dict) -> Dict:
        """PayPal Webhook-Event verarbeiten.

        Relevante Events:
          - PAYMENT.SALE.COMPLETED → Abo-Zahlung eingegangen → verlaengern
          - BILLING.SUBSCRIPTION.CANCELLED → Abo gekuendigt
          - BILLING.SUBSCRIPTION.SUSPENDED → Zahlung fehlgeschlagen
          - BILLING.SUBSCRIPTION.ACTIVATED → Abo aktiviert

        Returns:
            {'action': str, 'hardware_id': str, 'plan': str, 'days': int}
        """
        logger.info(f"PayPal Webhook empfangen: {event_type}")

        if event_type == 'PAYMENT.SALE.COMPLETED':
            subscription_id = resource.get('billing_agreement_id')
            if subscription_id:
                sub = self.get_subscription_status(subscription_id)
                hardware_id = sub.get('custom_id', '')
                plan_id = sub.get('plan_id', '')

                if plan_id == self.monthly_plan_id:
                    plan = 'monthly'
                    days = self.PLANS['monthly']['license_days']
                elif plan_id == self.yearly_plan_id:
                    plan = 'yearly'
                    days = self.PLANS['yearly']['license_days']
                else:
                    plan = 'unknown'
                    days = 35

                return {
                    'action': 'extend',
                    'hardware_id': hardware_id,
                    'subscription_id': subscription_id,
                    'plan': plan,
                    'days': days,
                }

        elif event_type == 'BILLING.SUBSCRIPTION.ACTIVATED':
            hardware_id = resource.get('custom_id', '')
            plan_id = resource.get('plan_id', '')
            plan = 'monthly' if plan_id == self.monthly_plan_id else 'yearly'
            days = self.PLANS.get(plan, {}).get('license_days', 35)
            return {
                'action': 'activate',
                'hardware_id': hardware_id,
                'subscription_id': resource.get('id'),
                'plan': plan,
                'days': days,
            }

        elif event_type in ('BILLING.SUBSCRIPTION.CANCELLED',
                            'BILLING.SUBSCRIPTION.EXPIRED'):
            hardware_id = resource.get('custom_id', '')
            return {
                'action': 'cancel',
                'hardware_id': hardware_id,
                'subscription_id': resource.get('id'),
            }

        elif event_type == 'BILLING.SUBSCRIPTION.SUSPENDED':
            hardware_id = resource.get('custom_id', '')
            return {
                'action': 'suspend',
                'hardware_id': hardware_id,
                'subscription_id': resource.get('id'),
            }

        return {'action': 'ignore', 'event_type': event_type}

    # ------------------------------------------------------------------
    # Lizenzschluessel generieren
    # ------------------------------------------------------------------

    @staticmethod
    def generate_license_key() -> str:
        """Neuen Lizenzschluessel im Format VE-XXXX-XXXX-XXXX generieren."""
        chars = string.ascii_uppercase + string.digits
        parts = [
            ''.join(secrets.choice(chars) for _ in range(4))
            for _ in range(3)
        ]
        return f"VE-{parts[0]}-{parts[1]}-{parts[2]}"

    # ------------------------------------------------------------------
    # Oeffentliche Infos (fuer Frontend)
    # ------------------------------------------------------------------

    def get_plans_info(self) -> Dict:
        """Planinfos fuer das Frontend."""
        return {
            'configured': self.is_configured,
            'mode': self.mode,
            'client_id': self.client_id if self.is_configured else None,
            'plans': {
                k: {
                    'name': v['name'],
                    'price': v['price'],
                    'currency': v['currency'],
                    'interval': v['interval'],
                }
                for k, v in self.PLANS.items()
            },
            'has_monthly_plan': bool(self.monthly_plan_id),
            'has_yearly_plan': bool(self.yearly_plan_id),
        }
