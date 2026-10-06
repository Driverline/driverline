//+------------------------------------------------------------------+
//| TradeLensCompanion.mq5                                          |
//| Companion for TradeLens.                                        |
//| Sends account state, open positions, closed trades and contract  |
//| specs to your TradeLens app and shows a risk panel on the chart.|
//| It NEVER opens a trade. It can CLOSE positions, but only when    |
//| InpAllowRemoteClose is on AND the account owner clicks Close in  |
//| the TradeLens app.                                              |
//+------------------------------------------------------------------+
#property copyright   "TradeLens by Driverline"
#property version     "1.10"
#property description "Syncs account state to TradeLens. Never opens trades. Closes positions only on request."

#include <Trade/Trade.mqh>

input string InpServerUrl   = "https://your-tradelens-address"; // TradeLens address, no trailing slash
input string InpKey         = "";                                // Connection key from TradeLens > MT5
input int    InpIntervalSec = 15;                                // Seconds between syncs (minimum 5)
input bool   InpShowPanel   = true;                              // Show the on-chart panel
input bool   InpSyncJournal = true;                              // Send closed trades to your TradeLens journal
input int    InpJournalDays = 7;                                 // How many days of closed trades to send at start
input bool   InpAllowRemoteClose = false;                        // Allow CLOSING positions from the TradeLens app (never opens trades)

const string PFX        = "DL_";
const color  CLR_BG     = C'8,10,22';
const color  CLR_PANEL  = C'15,20,50';
const color  CLR_ACCENT = C'30,60,120';

bool     g_collapsed = false;
bool     g_paused    = false;
string   g_status    = "WAIT";
string   g_l1        = "Waiting for first sync...";
string   g_l2        = "";
string   g_l3        = "";
string   g_net       = "Starting";
datetime g_lastSync  = 0;
datetime g_lastSpecs = 0;
datetime g_lastFull  = 0;
datetime g_jSince    = 0;
string   g_cmdMsg    = "";
CTrade   g_trade;

//+------------------------------------------------------------------+
//| JSON helpers                                                     |
//+------------------------------------------------------------------+
string JsonEscape(string s)
{
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "\"", "\\\"");
   return s;
}

string JsonGet(const string json, const string key)
{
   string pats[2];
   pats[0] = "\"" + key + "\":\"";
   pats[1] = "\"" + key + "\": \"";
   for(int i = 0; i < 2; i++)
   {
      int p = StringFind(json, pats[i]);
      if(p < 0)
         continue;
      p += StringLen(pats[i]);
      int e = StringFind(json, "\"", p);
      if(e < 0)
         return "";
      return StringSubstr(json, p, e - p);
   }
   return "";
}

long JsonGetNum(const string json, const string key)
{
   string pat = "\"" + key + "\":";
   int p = StringFind(json, pat);
   if(p < 0)
      return 0;
   p += StringLen(pat);
   int len = StringLen(json);
   while(p < len && StringGetCharacter(json, p) == ' ')
      p++;
   int e = p;
   while(e < len)
   {
      ushort c = StringGetCharacter(json, e);
      if(c < '0' || c > '9')
         break;
      e++;
   }
   return StringToInteger(StringSubstr(json, p, e - p));
}

string BaseUrl()
{
   string u = InpServerUrl;
   StringTrimLeft(u);
   StringTrimRight(u);
   while(StringLen(u) > 0 && StringGetCharacter(u, StringLen(u) - 1) == '/')
      u = StringSubstr(u, 0, StringLen(u) - 1);
   return u;
}

//+------------------------------------------------------------------+
//| Account data                                                     |
//+------------------------------------------------------------------+
double PositionRisk(const string sym, const long type, const double vol, const double open, const double sl)
{
   if(sl <= 0.0)
      return 0.0;
   double p = 0.0;
   ENUM_ORDER_TYPE ot = (type == POSITION_TYPE_BUY) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   if(!OrderCalcProfit(ot, sym, vol, open, sl, p))
      return 0.0;
   return MathAbs(MathMin(p, 0.0));
}

void TodayClosed(double &pnl, int &count)
{
   pnl = 0.0;
   count = 0;
   datetime now   = TimeCurrent();
   datetime start = now - (now % 86400);
   if(!HistorySelect(start, now))
      return;
   int n = HistoryDealsTotal();
   for(int i = 0; i < n; i++)
   {
      ulong t = HistoryDealGetTicket(i);
      if(t == 0)
         continue;
      long dtype = HistoryDealGetInteger(t, DEAL_TYPE);
      if(dtype != DEAL_TYPE_BUY && dtype != DEAL_TYPE_SELL)
         continue;
      long entry = HistoryDealGetInteger(t, DEAL_ENTRY);
      if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_INOUT && entry != DEAL_ENTRY_OUT_BY)
         continue;
      pnl += HistoryDealGetDouble(t, DEAL_PROFIT) + HistoryDealGetDouble(t, DEAL_SWAP)
           + HistoryDealGetDouble(t, DEAL_COMMISSION) + HistoryDealGetDouble(t, DEAL_FEE);
      count++;
   }
}

// Fully closed positions since g_jSince, as JSON rows. Two passes: HistorySelectByPosition
// replaces the selected history, so the position ids are collected first.
string BuildClosed(datetime &newest)
{
   newest = g_jSince;
   datetime now = TimeCurrent();
   if(!HistorySelect(g_jSince, now))
      return "";
   ulong ids[];
   int   nIds = 0;
   int   total = HistoryDealsTotal();
   for(int i = 0; i < total && nIds < 50; i++)
   {
      ulong t = HistoryDealGetTicket(i);
      if(t == 0)
         continue;
      long dtype = HistoryDealGetInteger(t, DEAL_TYPE);
      if(dtype != DEAL_TYPE_BUY && dtype != DEAL_TYPE_SELL)
         continue;
      if(HistoryDealGetInteger(t, DEAL_ENTRY) != DEAL_ENTRY_OUT)
         continue;
      ulong pid = (ulong)HistoryDealGetInteger(t, DEAL_POSITION_ID);
      bool dup = false;
      for(int k = 0; k < nIds; k++)
         if(ids[k] == pid)
            dup = true;
      if(dup)
         continue;
      ArrayResize(ids, nIds + 1);
      ids[nIds] = pid;
      nIds++;
   }

   string rows = "";
   int    added = 0;
   for(int n = 0; n < nIds; n++)
   {
      ulong pid = ids[n];
      if(PositionSelectByTicket(pid))
         continue;   // still open (partial close)
      if(!HistorySelectByPosition(pid))
         continue;
      double   inVol = 0.0, inPx = 0.0, outVol = 0.0, outPx = 0.0, net = 0.0;
      datetime tOpen = 0, tClose = 0;
      long     dir = -1;
      string   sym = "";
      ulong    inOrder = 0;
      int      m = HistoryDealsTotal();
      for(int j = 0; j < m; j++)
      {
         ulong d = HistoryDealGetTicket(j);
         if(d == 0)
            continue;
         long ty = HistoryDealGetInteger(d, DEAL_TYPE);
         if(ty != DEAL_TYPE_BUY && ty != DEAL_TYPE_SELL)
            continue;
         long   en = HistoryDealGetInteger(d, DEAL_ENTRY);
         double v  = HistoryDealGetDouble(d, DEAL_VOLUME);
         double px = HistoryDealGetDouble(d, DEAL_PRICE);
         net += HistoryDealGetDouble(d, DEAL_PROFIT) + HistoryDealGetDouble(d, DEAL_SWAP)
              + HistoryDealGetDouble(d, DEAL_COMMISSION) + HistoryDealGetDouble(d, DEAL_FEE);
         if(en == DEAL_ENTRY_IN)
         {
            inVol += v;
            inPx  += px * v;
            if(tOpen == 0)
            {
               tOpen   = (datetime)HistoryDealGetInteger(d, DEAL_TIME);
               dir     = ty;
               inOrder = (ulong)HistoryDealGetInteger(d, DEAL_ORDER);
               sym     = HistoryDealGetString(d, DEAL_SYMBOL);
            }
         }
         else if(en == DEAL_ENTRY_OUT || en == DEAL_ENTRY_OUT_BY || en == DEAL_ENTRY_INOUT)
         {
            outVol += v;
            outPx  += px * v;
            tClose  = (datetime)HistoryDealGetInteger(d, DEAL_TIME);
         }
      }
      if(inVol <= 0.0 || outVol <= 0.0 || dir < 0 || StringLen(sym) == 0)
         continue;
      double sl = 0.0, tp = 0.0;
      if(inOrder > 0)
      {
         sl = HistoryOrderGetDouble(inOrder, ORDER_SL);
         tp = HistoryOrderGetDouble(inOrder, ORDER_TP);
      }
      int dg = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      if(added > 0)
         rows += ",";
      rows += "{\"id\":" + (string)pid
            + ",\"symbol\":\"" + JsonEscape(sym) + "\""
            + ",\"type\":\"" + ((dir == DEAL_TYPE_BUY) ? "BUY" : "SELL") + "\""
            + ",\"volume\":" + DoubleToString(inVol, 4)
            + ",\"open\":" + DoubleToString(inPx / inVol, dg)
            + ",\"close\":" + DoubleToString(outPx / outVol, dg)
            + ",\"sl\":" + DoubleToString(sl, dg)
            + ",\"tp\":" + DoubleToString(tp, dg)
            + ",\"profit\":" + DoubleToString(net, 2)
            + ",\"opened\":" + (string)(long)tOpen
            + ",\"closed\":" + (string)(long)tClose + "}";
      added++;
      if(tClose > newest)
         newest = tClose;
   }
   return rows;
}

string BuildSpecs()
{
   string s = "";
   int added = 0;
   int total = SymbolsTotal(true);
   for(int i = 0; i < total && added < 80; i++)
   {
      string sym = SymbolName(i, true);
      if(StringLen(sym) == 0)
         continue;
      if(added > 0)
         s += ",";
      s += "{\"symbol\":\"" + JsonEscape(sym) + "\""
         + ",\"tick_size\":"     + DoubleToString(SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_SIZE), 8)
         + ",\"tick_value\":"    + DoubleToString(SymbolInfoDouble(sym, SYMBOL_TRADE_TICK_VALUE_LOSS), 8)
         + ",\"vol_min\":"       + DoubleToString(SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN), 8)
         + ",\"vol_step\":"      + DoubleToString(SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP), 8)
         + ",\"vol_max\":"       + DoubleToString(SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX), 8)
         + ",\"contract_size\":" + DoubleToString(SymbolInfoDouble(sym, SYMBOL_TRADE_CONTRACT_SIZE), 8) + "}";
      added++;
   }
   return s;
}

//+------------------------------------------------------------------+
//| Collect local numbers, then report to TradeLens                 |
//+------------------------------------------------------------------+
bool Sync()
{
   double closedPnl = 0.0;
   int    closedCnt = 0;
   TodayClosed(closedPnl, closedCnt);

   double floating  = 0.0;
   double totalRisk = 0.0;
   string pos       = "";
   int    total     = PositionsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong tk = PositionGetTicket(i);   // also selects the position
      if(tk == 0)
         continue;
      string sym  = PositionGetString(POSITION_SYMBOL);
      long   type = PositionGetInteger(POSITION_TYPE);
      double vol  = PositionGetDouble(POSITION_VOLUME);
      double op   = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl   = PositionGetDouble(POSITION_SL);
      double tp   = PositionGetDouble(POSITION_TP);
      double pr   = PositionGetDouble(POSITION_PROFIT) + PositionGetDouble(POSITION_SWAP);
      double risk = PositionRisk(sym, type, vol, op, sl);
      int    dg   = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      floating  += pr;
      totalRisk += risk;
      if(StringLen(pos) > 0)
         pos += ",";
      pos += "{\"ticket\":" + (string)tk
           + ",\"symbol\":\"" + JsonEscape(sym) + "\""
           + ",\"type\":\"" + ((type == POSITION_TYPE_BUY) ? "BUY" : "SELL") + "\""
           + ",\"volume\":" + DoubleToString(vol, 4)
           + ",\"open\":" + DoubleToString(op, dg)
           + ",\"sl\":" + DoubleToString(sl, dg)
           + ",\"tp\":" + DoubleToString(tp, dg)
           + ",\"profit\":" + DoubleToString(pr, 2)
           + ",\"risk\":" + DoubleToString(risk, 2) + "}";
   }

   // Local numbers: the panel stays useful even when the server cannot be reached.
   g_l1 = "Open " + (string)total + " | At risk " + DoubleToString(totalRisk, 2);
   g_l2 = "Today " + DoubleToString(closedPnl + floating, 2) + " " + AccountInfoString(ACCOUNT_CURRENCY);
   g_l3 = "Offline mode (local numbers only)";

   if(StringLen(InpKey) < 8)
   {
      g_net = "Paste your connection key in the EA inputs";
      return false;
   }
   if(StringFind(InpServerUrl, "your-tradelens") >= 0 || StringLen(BaseUrl()) < 8)
   {
      g_net = "Set the TradeLens address in the EA inputs";
      return false;
   }

   bool sendSpecs = (g_lastSpecs == 0 || TimeCurrent() - g_lastSpecs >= 1800);
   bool demo = (AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_DEMO);

   string body = "{\"v\":1,\"account\":{"
      + "\"login\":" + (string)AccountInfoInteger(ACCOUNT_LOGIN)
      + ",\"server\":\"" + JsonEscape(AccountInfoString(ACCOUNT_SERVER)) + "\""
      + ",\"currency\":\"" + JsonEscape(AccountInfoString(ACCOUNT_CURRENCY)) + "\""
      + ",\"balance\":" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 2)
      + ",\"equity\":" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 2)
      + ",\"margin_free\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2)
      + ",\"demo\":" + (demo ? "true" : "false")
      + ",\"remote_close\":" + (InpAllowRemoteClose ? "true" : "false")
      + ",\"server_offset\":" + (string)((int)MathRound((double)(TimeCurrent() - TimeGMT()) / 900.0) * 900) + "}"
      + ",\"today\":{\"closed_pnl\":" + DoubleToString(closedPnl, 2) + ",\"trades\":" + (string)closedCnt + "}"
      + ",\"positions\":[" + pos + "]";
   if(sendSpecs)
      body += ",\"specs\":[" + BuildSpecs() + "]";
   datetime newestClosed = g_jSince;
   if(InpSyncJournal)
   {
      string closedRows = BuildClosed(newestClosed);
      if(StringLen(closedRows) > 0)
         body += ",\"closed\":[" + closedRows + "]";
   }
   body += "}";

   char data[];
   char result[];
   int  n = StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   if(n > 0)
      ArrayResize(data, n - 1);   // drop the terminating zero

   string headers = "Content-Type: application/json\r\nX-Driverline-Key: " + InpKey + "\r\n";
   string resHeaders;
   ResetLastError();
   int code = WebRequest("POST", BaseUrl() + "/api/ea/report", headers, 8000, data, result, resHeaders);
   if(code == -1)
   {
      int err = GetLastError();
      if(err == 4014)
         g_net = "Allow the address: Tools > Options > Expert Advisors > Allow WebRequest";
      else
         g_net = "Network error " + (string)err;
      return false;
   }

   string resp = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   if(code != 200)
   {
      string why = JsonGet(resp, "detail");
      g_net = "Server replied " + (string)code + (StringLen(why) > 0 ? ": " + why : "");
      return false;
   }

   g_status   = JsonGet(resp, "status");
   g_l1       = JsonGet(resp, "line1");
   g_l2       = JsonGet(resp, "line2");
   g_l3       = JsonGet(resp, "line3");
   g_lastSync = TimeLocal();
   g_net      = "Connected";
   if(sendSpecs)
      g_lastSpecs = TimeCurrent();
   if(newestClosed > g_jSince)
      g_jSince = newestClosed + 1;
   return true;
}

//+------------------------------------------------------------------+
//| Remote close (only when InpAllowRemoteClose is on)               |
//+------------------------------------------------------------------+
bool ClosePos(const ulong ticket, string &err)
{
   if(!PositionSelectByTicket(ticket))
   {
      err = "Position not found";
      return false;
   }
   g_trade.SetTypeFillingBySymbol(PositionGetString(POSITION_SYMBOL));
   ResetLastError();
   bool ok = g_trade.PositionClose(ticket);
   if(!ok || g_trade.ResultRetcode() != TRADE_RETCODE_DONE)
   {
      err = g_trade.ResultRetcodeDescription();
      return false;
   }
   return true;
}

void ReportResult(const long id, const bool ok, const int closed, const int failed, const string msg)
{
   string body = "{\"id\":" + (string)id + ",\"ok\":" + (ok ? "true" : "false")
               + ",\"closed\":" + (string)closed + ",\"failed\":" + (string)failed
               + ",\"message\":\"" + JsonEscape(msg) + "\"}";
   char data[];
   char result[];
   int  n = StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   if(n > 0)
      ArrayResize(data, n - 1);
   string headers = "Content-Type: application/json\r\nX-Driverline-Key: " + InpKey + "\r\n";
   string rh;
   WebRequest("POST", BaseUrl() + "/api/ea/result", headers, 5000, data, result, rh);
}

void PollCommand()
{
   if(!InpAllowRemoteClose)
      return;
   char data[];
   char result[];
   string headers = "Content-Type: application/json\r\nX-Driverline-Key: " + InpKey + "\r\n";
   string rh;
   ResetLastError();
   int code = WebRequest("POST", BaseUrl() + "/api/ea/poll", headers, 5000, data, result, rh);
   if(code != 200)
      return;
   string resp = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   string cmd  = JsonGet(resp, "cmd");
   if(StringLen(cmd) == 0)
      return;
   long id     = JsonGetNum(resp, "id");
   long ticket = JsonGetNum(resp, "ticket");

   int    closed = 0;
   int    failed = 0;
   string msg    = "";
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
   {
      failed = 1;
      msg = "Turn on Algo Trading in MT5";
   }
   else if(cmd == "close")
   {
      string err = "";
      if(ClosePos((ulong)ticket, err))
         closed++;
      else
      {
         failed++;
         msg = err;
      }
   }
   else if(cmd == "close_all")
   {
      int   n = PositionsTotal();
      ulong list[];
      ArrayResize(list, n);
      for(int i = 0; i < n; i++)
         list[i] = PositionGetTicket(i);
      for(int i = 0; i < n; i++)
      {
         if(list[i] == 0)
            continue;
         string err = "";
         if(ClosePos(list[i], err))
            closed++;
         else
         {
            failed++;
            msg = err;
         }
      }
   }
   else
   {
      failed = 1;
      msg = "Unknown command";
   }

   g_cmdMsg = "Last close: " + (string)closed + " closed" + (failed > 0 ? ", " + (string)failed + " failed" : "")
            + (StringLen(msg) > 0 ? " (" + msg + ")" : "");
   ReportResult(id, (failed == 0), closed, failed, msg);
   g_lastFull = 0;   // refresh positions on the next tick
}

//+------------------------------------------------------------------+
//| Panel                                                            |
//+------------------------------------------------------------------+
void Rect(const string name, const int x, const int y, const int w, const int h, const color bg, const color border)
{
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, name, OBJPROP_XSIZE, w);
   ObjectSetInteger(0, name, OBJPROP_YSIZE, h);
   ObjectSetInteger(0, name, OBJPROP_BGCOLOR, bg);
   ObjectSetInteger(0, name, OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSetInteger(0, name, OBJPROP_BORDER_COLOR, border);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
}

void Lbl(const string name, const int x, const int y, const string text, const color clr, const int size)
{
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, size);
   ObjectSetString(0, name, OBJPROP_FONT, "Arial");
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
}

void Btn(const string name, const int x, const int y, const int w, const int h, const string text)
{
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_BUTTON, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, name, OBJPROP_XSIZE, w);
   ObjectSetInteger(0, name, OBJPROP_YSIZE, h);
   ObjectSetInteger(0, name, OBJPROP_BGCOLOR, CLR_ACCENT);
   ObjectSetInteger(0, name, OBJPROP_BORDER_COLOR, CLR_ACCENT);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clrWhite);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 8);
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetInteger(0, name, OBJPROP_STATE, false);
}

void DrawPanel()
{
   if(!InpShowPanel)
   {
      ObjectsDeleteAll(0, PFX);
      return;
   }
   const int px = 10;
   const int py = 20;
   const int w  = 300;
   int h = g_collapsed ? 34 : 166;

   Rect(PFX + "bg", px, py, w, h, CLR_BG, CLR_ACCENT);
   Rect(PFX + "hdr", px + 1, py + 1, w - 2, 32, CLR_PANEL, CLR_PANEL);
   Lbl(PFX + "title", px + 10, py + 8, "TRADELENS", clrDeepSkyBlue, 10);
   Btn(PFX + "btn_collapse", px + w - 112, py + 6, 50, 22, g_collapsed ? "Show" : "Hide");
   Btn(PFX + "btn_pause", px + w - 58, py + 6, 50, 22, g_paused ? "Resume" : "Pause");

   if(g_collapsed)
   {
      ObjectDelete(0, PFX + "status");
      ObjectDelete(0, PFX + "l1");
      ObjectDelete(0, PFX + "l2");
      ObjectDelete(0, PFX + "l3");
      ObjectDelete(0, PFX + "net");
      ObjectDelete(0, PFX + "cmd");
      return;
   }

   color sc = clrSilver;
   if(g_status == "OK")
      sc = clrLimeGreen;
   else if(g_status == "WARN")
      sc = clrGold;
   else if(g_status == "STOP")
      sc = clrTomato;

   string netText = (g_net == "Connected" && g_lastSync > 0) ? "Synced " + TimeToString(g_lastSync, TIME_SECONDS) : g_net;
   color  nc      = (g_net == "Connected") ? clrSilver : clrTomato;
   if(g_paused)
   {
      netText = "Paused (not sending)";
      nc = clrGold;
   }

   Lbl(PFX + "status", px + 10, py + 40, "STATUS: " + g_status, sc, 10);
   Lbl(PFX + "l1", px + 10, py + 62, g_l1, clrWhite, 9);
   Lbl(PFX + "l2", px + 10, py + 82, g_l2, clrWhite, 9);
   Lbl(PFX + "l3", px + 10, py + 102, g_l3, sc, 9);
   Lbl(PFX + "net", px + 10, py + 126, netText, nc, 8);
   Lbl(PFX + "cmd", px + 10, py + 144, (StringLen(g_cmdMsg) > 0 ? g_cmdMsg : (InpAllowRemoteClose ? "Remote close: ON" : "Remote close: off")), clrSilver, 8);
}

//+------------------------------------------------------------------+
int OnInit()
{
   g_trade.SetDeviationInPoints(200);
   g_jSince = TimeCurrent() - (datetime)(MathMax(1, InpJournalDays) * 86400);
   EventSetMillisecondTimer(3000);
   DrawPanel();
   OnTimer();
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   ObjectsDeleteAll(0, PFX);
}

void OnTimer()
{
   if(!g_paused)
   {
      datetime now = TimeLocal();
      if(g_lastFull == 0 || now - g_lastFull >= MathMax(5, InpIntervalSec))
      {
         g_lastFull = now;
         Sync();
      }
      else if(InpAllowRemoteClose && g_net == "Connected")
         PollCommand();
   }
   DrawPanel();
   ChartRedraw();
}

void OnTick()
{
}

void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
{
   if(id != CHARTEVENT_OBJECT_CLICK)
      return;
   if(sparam == PFX + "btn_collapse")
   {
      g_collapsed = !g_collapsed;
      ObjectSetInteger(0, sparam, OBJPROP_STATE, false);
      DrawPanel();
      ChartRedraw();
   }
   else if(sparam == PFX + "btn_pause")
   {
      g_paused = !g_paused;
      ObjectSetInteger(0, sparam, OBJPROP_STATE, false);
      if(!g_paused)
         g_lastFull = 0;
      DrawPanel();
      ChartRedraw();
   }
}
//+------------------------------------------------------------------+
