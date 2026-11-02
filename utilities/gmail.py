import base64
import re
import json
import uuid
import email as email_lib
from email import policy as email_policy
from email.utils import parsedate_to_datetime
from sqlmodel import select

from datetime import datetime, timezone

from config import Config
from utilities.pdf_processor import ExtractHoldings
from utilities.reconcile import process_and_reconcile_from_cas
from database import db_handler, User, Holdings, Transaction, IPO, Stock

class GetHoldingsFromGmail:
    def __init__(self):
        self.extractor = ExtractHoldings()
        self.user_id = None
        self.PASSWORD = None
        self.zerodha_query = "from:no-reply-transaction-with-holding-statement@reportsmailer.zerodha.net"
        self.cas_query = "from:eCAS@cdslstatement.com"
        self.nse_query = "from:nse-direct@nse.co.in"
        self.bse_query = "from:mgrpt@bseindia.com"
        self.STATEMENT_SENDERS = {"ecas@cdslstatement.com", "nse-direct@nse.co.in", "nse-direct@uci.nse.co.in", "mgrpt@bseindia.com"}

    def _get_user_details(self, user_id: str):
        with db_handler.get_session() as session:
            user = session.exec(select(User).where(User.user_id == user_id)).first()
            self.PASSWORD = (user.pan_card or "").strip().upper() # type: ignore

    def get_attachments_in_memory(self, msg):
        """Collect attachments from a parsed email. Attached emails (message/rfc822) are returned
        as .eml bytes and not descended into; process_attachment_recursive handles them."""
        attachments = []

        def walk(part):
            content_type = part.get_content_type()
            filename = part.get_filename()
            if content_type == "message/rfc822":
                inner = part.get_payload(0)
                attachments.append({"filename": filename or "forwarded.eml", "data": inner.as_bytes()})
            elif part.is_multipart():
                for sub in part.iter_parts():
                    walk(sub)
            elif filename:
                data = part.get_payload(decode=True)
                if data:
                    attachments.append({"filename": filename, "data": data})

        walk(msg)
        return attachments

    def _user_email_candidates(self, msg):
        """Addresses that may identify the user, most reliable first, excluding our own inboxes.

        Mail reaches us via user's Gmail -> (auto-forward) -> transactions@knotwealth.com ->
        (Cloudflare) -> our Gmail, so each hop adds headers. The original recipient is in
        X-Forwarded-For (Gmail auto-forward) or the oldest Delivered-To; for a manual forward
        the user is the From address.
        """
        def addresses(header):
            found = []
            for value in msg.get_all(header, []):
                found.extend(re.findall(r'[\w\.+-]+@[\w\.-]+', str(value).lower()))
            return found

        ordered = (
            addresses("X-Forwarded-For")
            + list(reversed(addresses("Delivered-To")))
            + addresses("X-Original-To")
            + addresses("To")
            + addresses("Cc")
            + addresses("From")
        )
        candidates = []
        for address in ordered:
            if address not in Config.INBOX_ADDRESSES and address not in candidates:
                candidates.append(address)
        return candidates

    def _get_full_body_text(self, msg):
        text = ""
        for part in msg.walk():
            if part.get_content_maintype() == "text" and not part.get_filename():
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    text += payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
        return text

    def _detect_broker_from_text(self, text: str):
        text_lower = text.lower()
        if "zerodha" in text_lower:
            return "Zerodha"
        if "groww" in text_lower:
            return "Groww"
        if "angleone" in text_lower or "angelone" in text_lower:
            return "AngelOne"
        return None

    def _extract_nse_date_from_text(self, full_text: str):
        match = re.search(r"for   (\d{1,2}-[A-Z]{3,}-\d{4})", full_text)
        if match:
            date_str = match.group(1)
            try:
                from datetime import datetime
                dt = datetime.strptime(date_str, "%d-%b-%Y")
                return dt.strftime("%Y-%m-%d")
            except:
                return date_str
        return None

    def _extract_info_from_eml(self, eml_data):
        msg = email_lib.message_from_bytes(eml_data, policy=email_policy.default)

        sender = ""
        # Check headers
        headers_to_check = ["X-Forwarded-For", "X-Original-From", "Return-Path", "From"]
        for h in headers_to_check:
            val = msg.get(h)
            if val:
                match = re.search(r'[\w\.-]+@[\w\.-]+', str(val))
                if match:
                    sender = match.group(0).lower()
                    break
        
        body_text = ""
        attachments = []
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = part.get('Content-Disposition')
            
            if content_type == "text/plain" and not disposition:
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    body_text += payload.decode(errors='ignore')
            elif disposition:
                filename = part.get_filename()
                if filename:
                    attachments.append({
                        "filename": filename,
                        "data": part.get_payload(decode=True)
                    })
        
        # Check body for "from:" (forwarded)
        if body_text:
            match = re.search(r"from:.*?([\w\.-]+@[\w\.-]+)", body_text, re.IGNORECASE)
            if match:
                sender = match.group(1).lower()

        # Date extraction
        eml_date = self._extract_nse_date_from_text(body_text)
        if not eml_date:
            date_header = msg.get("Date")
            if date_header:
                try:
                    import email.utils
                    dt_tuple = email.utils.parsedate_tz(str(date_header))
                    if dt_tuple:
                        from datetime import datetime
                        dt = datetime.fromtimestamp(email.utils.mktime_tz(dt_tuple))
                        eml_date = dt.strftime("%Y-%m-%d")
                except:
                    pass
                    
        return {
            "sender": sender,
            "date": eml_date,
            "attachments": attachments,
            "body": body_text
        }

    # ------------------------------------------------------------------
    # Core incremental processing
    # ------------------------------------------------------------------

    def _fetch_ipo_price(self, isin: str) -> float:
        with db_handler.get_session() as session:
            ipo_detail = session.exec(
                        select(IPO).where(
                            IPO.isin_code == isin
                        )
                    ).first()
            if ipo_detail:
                return ipo_detail.offer_price, ipo_detail.ipo_listing_date
        return 0, None # type: ignore

    def _save_transactions(self, extractions):
        if not extractions:
            return

        with db_handler.get_session() as session:
            for item in extractions:
                symbol = item.get("symbol")
                if not symbol:
                    continue
                try:
                    qty_val = float(str(item.get("quantity", 0)).replace(",", ""))
                    rate = float(str(item.get("rate", 0)).replace(",", ""))
                except ValueError:
                    continue

                trade_datetime = item.get("trade_datetime")
                if isinstance(trade_datetime, str):
                    try:
                        trade_datetime = datetime.fromisoformat(trade_datetime.replace("Z", "+00:00"))
                    except Exception:
                        try:
                            trade_datetime = datetime.strptime(trade_datetime, "%Y-%m-%d %H:%M:%S%z")
                        except Exception:
                            trade_datetime = datetime.now()
                elif not trade_datetime:
                    trade_datetime = datetime.now()
                
                if trade_datetime.tzinfo:
                    trade_datetime = trade_datetime.replace(tzinfo=None)

                # skip duplicates
                existing = session.exec(
                    select(Transaction).where(
                        Transaction.user_id == self.user_id,
                        Transaction.stock_symbol == symbol,
                        Transaction.transaction_type == item.get("transaction_type", "BUY").upper(),
                        Transaction.quantity == int(qty_val),
                        Transaction.price == rate
                    )
                ).all()

                found = False
                for ex in existing:
                    if abs((ex.transaction_datetime - trade_datetime).total_seconds()) < 86400:
                        found = True
                        break

                if found:
                    continue

                transaction = Transaction(
                    transaction_id=str(uuid.uuid4()),
                    user_id=self.user_id,
                    transaction_datetime=trade_datetime,
                    stock_symbol=symbol,
                    stock_name=item.get("company_name", ""),
                    transaction_type=item.get("transaction_type", "BUY").upper(),
                    quantity=int(qty_val),
                    broker=item.get("broker", ""),
                    exchange=item.get("exchange", ""),
                    price=rate,
                    inferred=item.get("is_inferred", False)
                )
                session.add(transaction)
            session.commit()

    def _calculate_and_upsert_holdings(self):
        with db_handler.get_session() as session:
            transactions = session.exec(
                select(Transaction).where(Transaction.user_id == self.user_id).order_by(Transaction.transaction_datetime)
            ).all()

            holdings_cache = {}
            transactions_to_add = []

            for t in transactions:
                base_symbol = t.stock_symbol.split(".")[0]
                if base_symbol not in holdings_cache:
                    holdings_cache[base_symbol] = Holdings(
                        holding_id=str(uuid.uuid4()),
                        user_id=self.user_id,
                        stock_symbol=base_symbol,
                        company_name=t.stock_name,
                        quantity=0,
                        avg_buy=0.0,
                        realized_pl=0.0,
                        holding_datetime=t.transaction_datetime
                    )

                holding = holdings_cache[base_symbol]
                old_qty = holding.quantity
                old_rate = holding.avg_buy

                if t.transaction_type == "BUY":
                    new_qty = old_qty + t.quantity
                    new_rate = ((old_qty * old_rate) + (t.quantity * t.price)) / new_qty if new_qty > 0 else 0
                    holding.quantity = new_qty
                    holding.avg_buy = float(f"{new_rate:.2f}")
                elif t.transaction_type == "SELL":
                    new_qty = old_qty - t.quantity
                    
                    if old_qty == 0:
                        stock = session.exec(select(Stock).where((Stock.nse_symbol == base_symbol) | (Stock.bse_symbol == base_symbol))).first()
                        isin = stock.isin_code if stock else ""
                        ipo_price, listing_date = self._fetch_ipo_price(isin)
                        
                        if ipo_price > 0:
                            old_rate = ipo_price
                            holding.avg_buy = ipo_price
                            ipo_transaction = Transaction(
                                transaction_id=str(uuid.uuid4()),
                                user_id=self.user_id,
                                transaction_datetime=listing_date if listing_date else t.transaction_datetime,
                                stock_symbol=t.stock_symbol,
                                stock_name=t.stock_name,
                                transaction_type='BUY',
                                quantity=t.quantity,
                                broker=t.broker,
                                exchange=t.exchange,
                                price=ipo_price,
                                inferred=True
                            )
                            transactions_to_add.append(ipo_transaction)
                        else:
                            old_rate = t.price
                    
                    realized_pl = (t.price - old_rate) * t.quantity
                    t.realized_pl = float(f"{realized_pl:.2f}")
                    session.add(t)
                    holding.quantity = new_qty
                    holding.realized_pl = float(f"{(holding.realized_pl + realized_pl):.2f}")

                holding.holding_datetime = t.transaction_datetime

            for ipo_txn in transactions_to_add:
                session.add(ipo_txn)

            existing_holdings = session.exec(select(Holdings).where(Holdings.user_id == self.user_id)).all()
            for h in existing_holdings:
                session.delete(h)
            
            for h in holdings_cache.values():
                session.add(h)
            session.commit()

    def process_transactions(self, message_id: str, raw_email: bytes):
        """Process email transactions.

        Args:
            message_id (str): The ID of the email message to process.
            raw_email (bytes): The full RFC 822 email, as sent by the Apps Script poller.
        """
        msg = email_lib.message_from_bytes(raw_email, policy=email_policy.default)

        sender = None
        match = re.search(r'[\w\.-]+@[\w\.-]+', str(msg.get("From", "")).lower())
        if match:
            sender = match.group(0)

        attachments = self.get_attachments_in_memory(msg)

        if not sender:
            print(f"Could not determine sender for message ID: {message_id}")
            return {'status': 500, 'message': f"Could not determine sender for message ID: {message_id}"}

        # Manually forwarded (not auto-forwarded) emails come From the user; the statement
        # sender is then in the "---------- Forwarded message ----------" block of the body
        if sender not in self.STATEMENT_SENDERS:
            forwarded = re.search(r"from:.*?([\w\.-]+@[\w\.-]+)", self._get_full_body_text(msg), re.IGNORECASE)
            if forwarded and forwarded.group(1).lower() in self.STATEMENT_SENDERS:
                sender = forwarded.group(1).lower()

        candidates = self._user_email_candidates(msg)
        print(f"User email candidates for message ID {message_id}: {candidates}")
        user_email = None
        with db_handler.get_session() as session:
            users = session.exec(select(User).where(User.email.in_(candidates))).all() if candidates else [] # type: ignore
            users_by_email = {u.email.lower(): u for u in users}
            for candidate in candidates:
                if candidate in users_by_email:
                    user_email = candidate
                    self.user_id = users_by_email[candidate].user_id
                    break
        if not self.user_id:
            print(f"No user found for message ID {message_id} among: {candidates}")
            return {'status': 202, 'message': f"No user found among: {candidates}"}
        self._get_user_details(self.user_id)

        print (f"Email from: {sender}, User email: {user_email}, Attachments found: {len(attachments)}")

        email_sent_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if msg.get("Date"):
            try:
                email_sent_date = parsedate_to_datetime(str(msg["Date"])).astimezone(timezone.utc).strftime("%Y-%m-%d")
            except Exception:
                pass
        body_text = self._get_full_body_text(msg)
        email_sent_date = self._extract_nse_date_from_text(body_text) or email_sent_date
        broker_name = self._detect_broker_from_text(body_text)
        
        any_new_data = False

        def process_attachment_recursive(current_sender, current_attachments, current_date, current_broker):
            nonlocal any_new_data
            for att in current_attachments:
                filename = str(att["filename"]).lower()
                if filename.endswith(".pdf"):
                    if current_sender == 'ecas@cdslstatement.com':
                        holdings = self.extractor.extract_cas_pdf(att["data"], self.PASSWORD)
                        new_txns = process_and_reconcile_from_cas(holdings, self.user_id)
                        if new_txns:
                            self._save_transactions(new_txns)
                            any_new_data = True
                    elif current_sender in ['nse-direct@nse.co.in', 'nse-direct@uci.nse.co.in']:
                        if current_date is None:
                            current_date = email_sent_date
                        extractions = self.extractor.extract_nse_pdf(att["data"], self.PASSWORD, current_date)
                        if extractions is not None:
                            self._save_transactions(extractions)
                            any_new_data = True
                    elif current_sender == 'mgrpt@bseindia.com':
                        extractions = self.extractor.extract_bse_pdf(att["data"], self.PASSWORD, current_broker)
                        if extractions is not None:
                            self._save_transactions(extractions)
                            any_new_data = True
                    else:
                        print(f"Unknown sender: {current_sender}, skipping attachment: {filename}")
                elif filename.endswith(".eml"):
                    print(f"Processing nested EML: {filename}")
                    eml_info = self._extract_info_from_eml(att["data"])
                    eml_broker = self._detect_broker_from_text(eml_info["body"]) or current_broker
                    process_attachment_recursive(
                        eml_info["sender"],
                        eml_info["attachments"],
                        eml_info["date"] or current_date,
                        eml_broker
                    )

        if attachments:
            process_attachment_recursive(sender, attachments, email_sent_date, broker_name)

            if any_new_data:
                self._calculate_and_upsert_holdings()

            return {'status': 200, 'message': "Processing completed."}
        else:
            print(f"No attachments found in message ID: {message_id}")
            return {'status': 202, 'message': "No attachments found."}
