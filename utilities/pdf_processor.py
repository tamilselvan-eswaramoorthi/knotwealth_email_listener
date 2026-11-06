import io
import re
import fitz
import pypdf
import camelot
import tempfile
import pandas as pd
from sqlmodel import select, or_
from datetime import date as date_type, datetime as datetime_type, timezone

from utilities.corporate_actions import (
    get_demerger_by_raw_symbol, get_demerger_by_bse_code,
    get_split, get_bonus
)
from database import db_handler, Stock
from utilities.casparser import CASParser

class ExtractHoldings:
    def __init__(self):
        self.pattern = re.compile(
            r"(IN[A-Z0-9]{10})\s+"
            r"(.*?)\s+"
            r"([\d\.]+)\s+"
            r"([\d\.]+)\s+"
            r"([\d\.]+)\s+"
            r"([\d\.]+)\s+"
            r"([\d\.]+)\s+"
            r"([\d\.]+)\s+"
            r"([\d\.]+(?:,\d+)?)\s+"
            r"([\d\.]+(?:,\d+)?)"
        )
        self.nse_columns = [
            "ISIN Code", "Company Name", "Curr. Bal", "Free Bal",
            "Pldg. Bal", "Earmark Bal", "Demat", "Remat",
            "Lockin", "Rate", "Value"
        ]
        self.bse_columns = [
            "ISIN Code", "Company Name", "Curr. Bal", "Free Bal",
            "Pldg. Bal", "Earmark Bal", "Demat", "Remat",
            "Lockin", "Rate", "Value"
        ]
        self.formats = [
            "%Y-%m-%d %H:%M:%S",
            "%d-%m-%Y %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%d/%m/%Y %H:%M:%S",
            "%Y-%m-%d %I:%M:%S %p",
            "%d-%m-%Y %I:%M:%S %p",
            "%Y/%m/%d %I:%M:%S %p",
            "%d/%m/%Y %I:%M:%S %p",
        ]

    # ======================================================================
    # Private helpers
    # ======================================================================

    def _parse_date_str(self, value) -> date_type:
        """
        Convert *value* (string, datetime, date, or None) to a ``date`` object.
        Falls back to today if parsing fails.
        """
        try:
            if isinstance(value, datetime_type):
                return value.date()
            if isinstance(value, date_type):
                return value
            if isinstance(value, str) and value.strip():
                s = value.strip()
                # Try ISO format first (most common from email_date)
                for fmt in ["%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%Y"]:
                    try:
                        return datetime_type.strptime(s, fmt).date()
                    except ValueError:
                        continue
        except Exception:
            pass
        return date_type.today()

    def _parse_trade_datetime(self, date_val, time_val, nse_fixed_fmt=False):
        """
        Build a UTC-aware datetime from *date_val* and *time_val*.

        Parameters
        ----------
        date_val      : date string or object
        time_val      : time string from the PDF row
        nse_fixed_fmt : if True, use the known NSE format "%Y-%m-%d %I:%M:%S %p"
                        instead of trying all self.formats (faster for NSE).
        """
        try:
            combined = f"{date_val} {time_val}".strip()
            if nse_fixed_fmt:
                dt = pd.to_datetime(combined, format="%Y-%m-%d %I:%M:%S %p", errors="coerce")
                if dt is not None and str(dt) != "NaT":
                    return dt.replace(tzinfo=timezone.utc)
                return None

            for fmt in self.formats:
                try:
                    dt = pd.to_datetime(combined, format=fmt, errors="coerce")
                    if dt is not None and str(dt) != "NaT":
                        return dt.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
        except Exception as e:
            print(f"Error parsing trade datetime: {e}")
        return None

    def _resolve_nse_symbol(self, query: str, company_name: str):
        """
        Query Stock table for *query* and return (preferred_symbol, name, isin).
        """
        query = str(query).strip().upper().split('.')[0].split()[0]
        try:
            with db_handler.get_session() as session:
                # Prefer exact match on symbol or ISIN
                stock = session.exec(
                    select(Stock).where(or_(Stock.nse_symbol == query, Stock.isin_code == query))
                ).first()
                if stock:
                    # Prefer NSE symbol, fallback to BSE symbol
                    preferred_symbol = stock.nse_symbol or stock.bse_symbol
                    return preferred_symbol or query, stock.name or company_name, stock.isin_code
        except Exception as e:
            print(f"[StockDB/NSE] Error resolving {query}: {e}")
        return query, company_name, None

    def _resolve_bse_symbol(self, scrip_code: str, company_name: str):
        """
        Query Stock table for *scrip_code* and return (preferred_symbol, name, isin).
        """
        scrip_code = str(scrip_code).strip()
        try:
            with db_handler.get_session() as session:
                # Scrip code is stored as bse_symbol (numeric 5xxxxx)
                stock = session.exec(
                    select(Stock).where(Stock.bse_symbol == scrip_code)
                ).first()
                if stock:
                    # Prefer NSE symbol, fallback to BSE symbol
                    preferred_symbol = stock.nse_symbol or stock.bse_symbol
                    return preferred_symbol, stock.name or company_name, stock.isin_code
        except Exception as e:
            print(f"[StockDB/BSE] Error resolving {scrip_code}: {e}")
        return scrip_code, company_name, None

    def _expand_demerger_children(self, demerger: dict, common_fields: dict) -> list:
        """
        Convert one demerger registry entry into a list of child row dicts.
        """
        rows = []
        qty   = common_fields["quantity"]
        price = float(common_fields["rate"])
        for child in demerger["children"]:
            child_symbol = child["symbol"]
            child_row = dict(common_fields)
            
            # Resolve the child symbol to get formal name and ISIN
            if common_fields.get("exchange") == "NSE":
                res_sym, res_name, res_isin = self._resolve_nse_symbol(child_symbol, child.get("company_name", ""))
            else:
                if str(child_symbol).isdigit():
                    res_sym, res_name, res_isin = self._resolve_bse_symbol(child_symbol, child.get("company_name", ""))
                else:
                    res_sym, res_name, res_isin = self._resolve_nse_symbol(child_symbol, child.get("company_name", ""))

            child_row["symbol"]       = res_sym
            child_row["company_name"] = res_name
            child_row["isin"]         = res_isin
            child_row["quantity"]     = int(qty * child["ratio"])
            child_row["rate"]         = round(price * child["price_ratio"] / child["ratio"], 2)
            rows.append(child_row)
        return rows

    # ======================================================================
    # Corporate-action expansion (post-Finnhub, for splits / bonuses)
    # ======================================================================

    def _expand_corporate_actions(self, row: dict, trade_date_str) -> list:
        """
        Check whether the already-resolved *row['symbol']* is subject to a
        split or bonus issue.  Demergers are handled pre-Finnhub via
        ``_expand_demerger_children``; this method is for the remaining cases.

        Returns a list of row dicts (normally just ``[row]``).
        """
        symbol = row.get("symbol", "")
        qty    = row.get("quantity", 0)
        price  = row.get("rate", 0)
        trade_date = self._parse_date_str(trade_date_str)

        # ── Split ──────────────────────────────────────────────────────────
        split = get_split(symbol, trade_date)
        if split:
            ratio     = split["ratio"]
            split_row = dict(row)
            split_row["quantity"] = int(qty * ratio)
            split_row["rate"]     = round(float(price) / ratio, 2)
            print(f"[Split] {symbol}: qty {qty}→{split_row['quantity']}, rate {price}→{split_row['rate']}")
            return [split_row]

        # ── Bonus ──────────────────────────────────────────────────────────
        bonus_rec = get_bonus(symbol, trade_date)
        if bonus_rec:
            bonus_qty = int(qty / bonus_rec["per"]) * bonus_rec["bonus"]
            if bonus_qty > 0:
                bonus_row = dict(row)
                bonus_row["quantity"] = bonus_qty
                bonus_row["rate"]     = 0.0
                bonus_row['description'] = "bonus"
                print(f"[Bonus] {symbol}: +{bonus_qty} bonus shares at rate=0")
                return [row, bonus_row]

        return [row]

    # ======================================================================
    # PDF extraction helpers
    # ======================================================================

    def _open_doc(self, pdf_source, password):
        """Open a PDF from bytes or a file path, authenticating if encrypted."""
        if isinstance(pdf_source, bytes):
            doc = fitz.open(stream=pdf_source, filetype="pdf")
        else:
            doc = fitz.open(pdf_source)
        if doc.is_encrypted:
            doc.authenticate(password)
        return doc
    
    def _check_password(self, pdf_source, password):
        """Fail with a clear message when the PAN does not unlock the PDF (camelot only says
        "File has not been decrypted")."""
        reader = pypdf.PdfReader(io.BytesIO(pdf_source))
        if reader.is_encrypted and not reader.decrypt(password or ""):
            raise Exception(
                f"PDF password did not open the statement: the user's pan_card is wrong "
                f"(length {len(password or '')}, ends {(password or '')[-2:]!r})"
            )

    def _extract_through_camelot(self, pdf_source, password):
        self._check_password(pdf_source, password)
        all_tables = []
        with tempfile.NamedTemporaryFile(suffix=".pdf") as temp_pdf:
            temp_pdf.write(pdf_source)
            temp_pdf.flush()
            tables = camelot.read_pdf(temp_pdf.name,  password=password, pages='all')
        if tables.n > 0:
            df = tables[0].df
            df.columns = df.iloc[0]
            df = df[1:]
            headers = [str(col).replace('\n', ' ') for col in df.columns]
            if any("Trade No" in h or "Symbol" in h for h in headers) and len(headers) >= 10:
                all_tables.append(df)
        return all_tables

    # ======================================================================
    # Public extraction methods
    # ======================================================================

    def extract_last_zerodha_holdings(self, pdf_source, password):
        doc = self._open_doc(pdf_source, password)
        holdings = []
        holding_date = None

        for page in doc:
            text = page.get_text("text").replace('\n', ' ')
            if not holding_date:
                date_match = re.search(r"Holdings as on (\d{4}-\d{2}-\d{2})", text)
                if date_match:
                    holding_date = date_match.group(1)
            matches = self.pattern.findall(text)
            for match in matches:
                holdings.append(match)

        doc.close()

        if holdings:
            df = pd.DataFrame(holdings, columns=self.nse_columns)
            df['holding_date'] = holding_date
            for index, row in df.iterrows():
                try:
                    isin = str(row['ISIN Code']).strip().upper()
                    with db_handler.get_session() as session:
                        stock = session.exec(select(Stock).where(Stock.isin_code == isin)).first()
                        if stock:
                            df.at[index, "Company Name"] = stock.name
                            # Prefer NSE symbol, fallback to BSE symbol
                            df.at[index, "Symbol"]       = stock.nse_symbol or stock.bse_symbol or stock.isin_code
                except Exception as e:
                    print(f"Error looking up stock from DB: {e}")
            df = df[['holding_date', 'Company Name', 'Symbol', 'Rate', 'Curr. Bal', 'ISIN Code']]
            df.rename(columns={
                'holding_date': 'timestamp', 'Company Name': 'company_name',
                'Symbol': 'symbol', 'Rate': 'rate', 'Curr. Bal': 'quantity',
                'ISIN Code': 'isin'
            }, inplace=True)
            res = df.to_dict(orient="records")
            # Filter out entries where quantity or rate is 0 or None
            return [r for r in res if r.get('quantity') and r.get('rate')]
        return None

    def _nse_trade_date(self, trade_no):
        """Return the trade date ("YYYY-MM-DD") encoded in an NSE trade number, or None."""
        match = re.match(r"(\d{8})\d+$", str(trade_no or "").strip())
        if not match:
            return None
        try:
            return datetime_type.strptime(match.group(1), "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            return None

    def extract_nse_pdf(self, pdf_source, password, email_date=None):
        """Extract trade rows from an NSE contract note PDF."""

        all_tables = self._extract_through_camelot(pdf_source, password)
        if not all_tables:
            return None

        final_df = pd.concat(all_tables, ignore_index=True)
        final_df.columns = [str(c).replace('\n', ' ').strip() for c in final_df.columns]
        rows = []
        for _, row in final_df.iterrows():
            try:
                # NSE trade numbers start with the trade date (YYYYMMDD); the email date is only a
                # fallback because a forwarded alert carries the forwarding date
                row_date = self._nse_trade_date(row.get('Trade No')) or email_date
                row_check_date = self._parse_date_str(row_date)
                raw_symbol   = str(row['Symbol']).strip()
                company_name = row.get('Name of the Security', raw_symbol)
                buy_or_sell  = "BUY" if row['Buy/ Sell'] == "B" else "SELL"
                broker_name = row.get('TM Name', '').strip()                
                if "GROWW" in broker_name.upper():
                    broker_name = "Groww"
                elif "ZERODHA" in broker_name.upper():
                    broker_name = "Zerodha"

                try:
                    quantity = int(row['Quantity'])
                except (ValueError, TypeError):
                    quantity = 0
                price          = row['Price (Rs.)']
                trade_datetime = self._parse_trade_datetime(row_date, row['Trade Time'], nse_fixed_fmt=True)

                early_demerger = get_demerger_by_raw_symbol(raw_symbol, row_check_date)
                if early_demerger:
                    common = {
                        "company_name":     company_name,
                        "quantity":         quantity,
                        "rate":             price,
                        "trade_datetime":   trade_datetime,
                        "transaction_type": buy_or_sell,
                        "exchange":         "NSE",
                        "broker":           broker_name,
                    }
                    rows.extend(self._expand_demerger_children(early_demerger, common))
                    continue 

                symbol, company_name, isin = self._resolve_nse_symbol(raw_symbol, company_name)
                base_row = {
                    "company_name":     company_name,
                    "symbol":           symbol,
                    "isin":             isin,
                    "rate":             price,
                    "quantity":         quantity,
                    "trade_datetime":   trade_datetime,
                    "transaction_type": buy_or_sell,
                    "exchange":         "NSE",
                    'broker':           broker_name,
                }
                rows.extend(self._expand_corporate_actions(base_row, row_date))

            except Exception as e:
                print(f"[NSE] Error processing row: {e}")

        rows = self._filter_rows(rows)
        return rows

    def extract_bse_pdf(self, pdf_source, password, broker_name=None):
        """Extract trade rows from a BSE contract note PDF."""

        all_tables = self._extract_through_camelot(pdf_source, password)

        if not all_tables:
            return None

        final_df = pd.concat(all_tables, ignore_index=True)
        final_df.columns = [str(c).replace('\n', ' ').strip() for c in final_df.columns]

        rows = []
        for _, row in final_df.iterrows():
            try:
                if not str(row.get('Scrip Name', '')).strip() and not str(row.get('Qty', '')).strip():
                    continue

                company_name = str(row['Scrip Name']).replace('\n', ' ').strip()
                scrip_code   = str(row['Scrip Code']).strip()
                buy_or_sell  = "BUY" if row['Buy Sell'] == "B" else "SELL"
                try:
                    quantity = int(row['Qty'])
                except (ValueError, TypeError):
                    quantity = 0
                price          = row['Price']
                trade_datetime = self._parse_trade_datetime(row['Trade Date'], row['Trade Time'])
                bse_trade_date = self._parse_date_str(row['Trade Date'])

                early_demerger = get_demerger_by_bse_code(scrip_code, bse_trade_date)
                if early_demerger:
                    common = {
                        "company_name":     company_name,
                        "quantity":         quantity,
                        "rate":             price,
                        "trade_datetime":   trade_datetime,
                        "transaction_type": buy_or_sell,
                        "exchange":         "BSE",
                        "broker":           broker_name
                    }
                    rows.extend(self._expand_demerger_children(early_demerger, common))
                    continue  

                symbol, company_name, isin = self._resolve_bse_symbol(scrip_code, company_name)
                base_row = {
                    "company_name":     company_name,
                    "symbol":           symbol,
                    "isin":             isin,
                    "rate":             price,
                    "quantity":         quantity,
                    "trade_datetime":   trade_datetime,
                    "transaction_type": buy_or_sell,
                    "exchange":         "BSE",
                    "broker":           broker_name,
                }
                rows.extend(self._expand_corporate_actions(base_row, None))

            except Exception as e:
                print(f"[BSE] Error processing row: {e}")
                
        rows = self._filter_rows(rows)
        return rows

    def _filter_rows(self, rows):
        filtered = []
        for r in rows:
            if 'description' in r and r['description'] == 'bonus':
                print(f"Including bonus row: {r}")
                filtered.append(r)
            elif r.get('quantity') and r.get('rate'):
                filtered.append(r)
            else:
               print(f"Filtering out row with missing quantity or rate: {r}")
        return filtered

    def extract_cas_pdf(self, pdf_source, password):
        doc = self._open_doc(pdf_source, password)
        all_holding = CASParser(doc).run()
        return all_holding