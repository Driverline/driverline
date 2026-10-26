//+------------------------------------------------------------------+
//| TradeLensCompanion.mq5                                          |
//| Companion for TradeLens.                                        |
//| Sends account state, open positions, closed trades and contract  |
//| specs to your TradeLens app and shows a risk panel on the chart.|
//| It NEVER opens a trade on its own. It opens one only when        |
//| InpAllowExecute is on AND the account owner clicks Open trade in |
//| the TradeLens app. It can CLOSE positions, but only when         |
//| InpAllowRemoteClose is on AND the owner clicks Close. With       |
//| InpAllowManage on it can also move a                             |
//| position's stop and take-profit (breakeven, trailing) using the  |
//| settings chosen in the app. It never widens a stop.              |
//| With InpTgEnable on it also posts your trades to your own        |
//| Telegram channel: entry, stop, take-profit, then breakeven,      |
//| trailing moves and the close. It only READS positions for this.  |
//| With InpAllowPending on it can also PLACE and CANCEL pending     |
//| orders (limit, stop, stop limit) that you set from a NO TRADE    |
//| card. MT5 itself fires them if price reaches the level, even if  |
//| the app is closed. Each has a stop, target and an expiry.        |
//+------------------------------------------------------------------+
#property copyright   "TradeLens by Driverline"
#property version     "1.51"
#property description "Syncs account state to TradeLens. Opens or closes a trade, or places a pending order, only when you click a button in the app and allowed it here."

#include <Trade/Trade.mqh>

input string InpServerUrl   = "https://your-tradelens-address"; // TradeLens address, no trailing slash
input string InpKey         = "";                                // Connection key from TradeLens > MT5
input int    InpIntervalSec = 15;                                // Seconds between syncs (minimum 5)
input bool   InpShowPanel   = true;                              // Show the on-chart panel
input bool   InpSyncJournal = true;                              // Send closed trades to your TradeLens journal
input int    InpJournalDays = 7;                                 // How many days of closed trades to send at start
input bool   InpAllowExecute = false;                            // Allow OPENING a trade when you click "Open trade" in the app (stop and target are always attached)
input double InpMaxLotsPerTrade = 0.0;                           // Extra safety: refuse orders above this lot size (0 = only the broker's own maximum)
input bool   InpAllowManage = false;                              // Allow auto-manage: breakeven, trailing stop and trailing take-profit chosen in the app (never widens a stop)
input bool   InpAllowPending = false;                            // Allow PLACING and CANCELLING pending orders (limit, stop, stop limit) that you set in the app. MT5 opens the trade later by itself if price reaches the level
input bool   InpAllowRemoteClose = false;                        // Allow CLOSING positions from the TradeLens app (never opens trades)

// ---- Telegram: post my trades to a channel (off by default)
input bool   InpTgEnable       = false;                          // Telegram: post my trades to a Telegram channel
input string InpTgToken        = "";                             // Telegram: bot token from @BotFather (keep it secret)
input string InpTgChatId       = "";                             // Telegram: channel, like @yourchannel or -100123456789
input bool   InpTgPostOpen     = true;                           // Telegram: post new trades (entry, SL, TP)
input bool   InpTgPostBE       = true;                           // Telegram: post when a stop moves to breakeven
input bool   InpTgPostTrailSL  = true;                           // Telegram: post trailing stop and other SL changes
input bool   InpTgPostTrailTP  = true;                           // Telegram: post take-profit moves
input bool   InpTgPostPartial  = true;                           // Telegram: post partial closes
input bool   InpTgPostClose    = true;                           // Telegram: post when a trade closes (result in R, never money)
input bool   InpTgShowLots     = false;                          // Telegram: show my lot size (off: members use their own size)
input bool   InpTgEmoji        = true;                           // Telegram: use emoji in posts
input int    InpTgTrailSecs    = 30;                             // Telegram: least seconds between trailing posts for one trade
input int    InpTgTrailStepPct = 10;                             // Telegram: least trailing step to post, in % of the original risk (1R)
input long   InpTgMagic        = -1;                             // Telegram: only post trades with this magic number (-1 = all trades)
input bool   InpTgAnnounceExisting = false;                      // Telegram: also announce trades already open when the EA starts
input bool   InpTgTestOnStart  = false;                          // Telegram: send a test message when the EA starts
input string InpTgHeader       = "TradeLens";                    // Telegram: name used in the test message
input string InpTgFooter       = "Educational only, not financial advice. Use your own lot size and risk."; // Telegram: line at the end of every post

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
string   g_mgMsg     = "";
// Auto-manage settings, sent by the app. Distances are in R (1R = the position's original stop distance).
bool     g_be_on = false;   double g_be_trigger = 1.0;  double g_be_offset = 0.1;
bool     g_tsl_on = false;  double g_tsl_start = 1.5;   double g_tsl_dist = 1.0;
bool     g_ttp_on = false;  double g_ttp_near = 0.3;    double g_ttp_step = 1.0;
string   g_excl = "";
CTrade   g_trade;

string BuildOrders();   // defined further down; Sync() uses it
string OrderStateText(const long st);   // defined further down; PlacePending() uses it

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

// Why a position closed, from MT5's own deal reason: tp, sl, manual (you, from the terminal, phone or web),
// expert (an EA or the TradeLens Close button), so (stop out by the broker) or other.
string ReasonName(const long r)
{
   if(r == (long)DEAL_REASON_TP)
      return "tp";
   if(r == (long)DEAL_REASON_SL)
      return "sl";
   if(r == (long)DEAL_REASON_SO)
      return "so";
   if(r == (long)DEAL_REASON_CLIENT || r == (long)DEAL_REASON_MOBILE || r == (long)DEAL_REASON_WEB)
      return "manual";
   if(r == (long)DEAL_REASON_EXPERT)
      return "expert";
   return "other";
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
      long     outReason = -1;
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
            outReason = HistoryDealGetInteger(d, DEAL_REASON);
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
            + ",\"closed\":" + (string)(long)tClose
            + ",\"reason\":\"" + ReasonName(outReason) + "\"}";
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

   string ord = BuildOrders();

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
      + ",\"manage_allowed\":" + (InpAllowManage ? "true" : "false")
      + ",\"execute_allowed\":" + (InpAllowExecute ? "true" : "false")
      + ",\"pending_allowed\":" + (InpAllowPending ? "true" : "false")
      + ",\"server_offset\":" + (string)((int)MathRound((double)(TimeCurrent() - TimeGMT()) / 900.0) * 900) + "}"
      + ",\"today\":{\"closed_pnl\":" + DoubleToString(closedPnl, 2) + ",\"trades\":" + (string)closedCnt + "}"
      + ",\"positions\":[" + pos + "]"
      + ",\"orders\":[" + ord + "]";
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
//| Auto-manage (only when InpAllowManage is on)                     |
//+------------------------------------------------------------------+
void ParseManage(const string resp)
{
   g_be_on      = (JsonGet(resp, "be_on") == "1");
   g_be_trigger = StringToDouble(JsonGet(resp, "be_trigger"));
   g_be_offset  = StringToDouble(JsonGet(resp, "be_offset"));
   g_tsl_on     = (JsonGet(resp, "tsl_on") == "1");
   g_tsl_start  = StringToDouble(JsonGet(resp, "tsl_start"));
   g_tsl_dist   = StringToDouble(JsonGet(resp, "tsl_dist"));
   g_ttp_on     = (JsonGet(resp, "ttp_on") == "1");
   g_ttp_near   = StringToDouble(JsonGet(resp, "ttp_near"));
   g_ttp_step   = StringToDouble(JsonGet(resp, "ttp_step"));
   g_excl       = JsonGet(resp, "excluded");
}

bool IsExcluded(const ulong ticket)
{
   return StringFind("," + g_excl + ",", "," + (string)ticket + ",") >= 0;
}

// The original stop distance (1R). It is remembered, because trailing moves the stop itself.
double InitialRisk(const ulong ticket, const double open, const double sl)
{
   string name = "TL_R_" + (string)ticket;
   if(GlobalVariableCheck(name))
      return GlobalVariableGet(name);
   if(sl <= 0.0)
      return 0.0;
   double r = MathAbs(open - sl);
   GlobalVariableSet(name, r);
   return r;
}

// Brokers refuse stops that are too close to the price. Returns false if the move would be refused.
bool StopsAllowed(const string sym, const long type, const double px, const double sl, const double tp)
{
   double pt   = SymbolInfoDouble(sym, SYMBOL_POINT);
   double minD = (double)SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL) * pt;
   double frz  = (double)SymbolInfoInteger(sym, SYMBOL_TRADE_FREEZE_LEVEL) * pt;
   double d    = (type == POSITION_TYPE_BUY) ? 1.0 : -1.0;
   if(sl > 0.0 && d * (px - sl) < MathMax(minD, frz) + pt)
      return false;
   if(tp > 0.0 && d * (tp - px) < MathMax(minD, frz) + pt)
      return false;
   return true;
}

void ManagePositions()
{
   if(!InpAllowManage || (!g_be_on && !g_tsl_on && !g_ttp_on))
      return;
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
      return;
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong tk = PositionGetTicket(i);
      if(tk == 0 || IsExcluded(tk))
         continue;
      string sym  = PositionGetString(POSITION_SYMBOL);
      long   type = PositionGetInteger(POSITION_TYPE);
      double open = PositionGetDouble(POSITION_PRICE_OPEN);
      double sl   = PositionGetDouble(POSITION_SL);
      double tp   = PositionGetDouble(POSITION_TP);
      double R    = InitialRisk(tk, open, sl);
      if(R <= 0.0)
         continue;               // no stop loss was ever set, so there is no 1R to work from
      double dir = (type == POSITION_TYPE_BUY) ? 1.0 : -1.0;
      double px  = (type == POSITION_TYPE_BUY) ? SymbolInfoDouble(sym, SYMBOL_BID) : SymbolInfoDouble(sym, SYMBOL_ASK);
      if(px <= 0.0)
         continue;
      int    dg      = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      double profitR = dir * (px - open) / R;
      double newSL   = sl;
      double newTP   = tp;
      string what    = "";

      if(g_be_on && profitR >= g_be_trigger)
      {
         double be = open + dir * g_be_offset * R;
         if(sl <= 0.0 || dir * (be - newSL) > 0.0)
         {
            newSL = be;
            what  = "breakeven";
         }
      }
      if(g_tsl_on && profitR >= g_tsl_start)
      {
         double t = px - dir * g_tsl_dist * R;
         if((newSL <= 0.0 || dir * (t - newSL) >= 0.05 * R))   // only forward, in steps of at least 0.05R
         {
            newSL = t;
            what  = "trailing stop";
         }
      }
      bool protectedPos = (newSL > 0.0) && (dir * (newSL - open) >= 0.0);
      if(g_ttp_on && tp > 0.0 && protectedPos && dir * (tp - px) <= g_ttp_near * R)
      {
         newTP = tp + dir * g_ttp_step * R;
         what += (StringLen(what) > 0 ? " + " : "") + "take-profit";
      }
      newSL = NormalizeDouble(newSL, dg);
      newTP = NormalizeDouble(newTP, dg);
      if(newSL == NormalizeDouble(sl, dg) && newTP == NormalizeDouble(tp, dg))
         continue;
      if(sl > 0.0 && dir * (newSL - sl) < 0.0)
         continue;               // never widen a stop
      if(!StopsAllowed(sym, type, px, newSL, newTP))
         continue;
      g_trade.SetTypeFillingBySymbol(sym);
      if(g_trade.PositionModify(tk, newSL, newTP) && g_trade.ResultRetcode() == TRADE_RETCODE_DONE)
         g_mgMsg = "Auto-manage: #" + (string)tk + " " + what;
   }
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

// Places ONE order that the member asked for with a click. Returns the message to show; ok tells if it worked.
string OpenTrade(const string sym, const string dirS, const double reqLots, const double slDist, const double tpDist,
                 const double ref, const double maxDev, const string cmt, bool &ok)
{
   ok = false;
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
      return "Turn on Algo Trading in MT5";
   if(StringLen(sym) == 0 || slDist <= 0.0 || tpDist <= 0.0 || (dirS != "BUY" && dirS != "SELL"))
      return "Incomplete order request";
   if(!SymbolSelect(sym, true))
      return sym + " is not available in your MT5";
   if(SymbolInfoInteger(sym, SYMBOL_TRADE_MODE) != SYMBOL_TRADE_MODE_FULL)
      return "Trading is not allowed on " + sym + " right now";
   bool   buy   = (dirS == "BUY");
   double price = buy ? SymbolInfoDouble(sym, SYMBOL_ASK) : SymbolInfoDouble(sym, SYMBOL_BID);
   if(price <= 0.0)
      return "No price for " + sym + " right now";
   if(maxDev > 0.0 && MathAbs(price - ref) > maxDev)
      return "Price moved too far from the setup. Check the chart before trading.";

   double vmin  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double vstep = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   double vmax  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   int    ld    = 0;
   for(double st = vstep; st < 1.0 - 1e-9 && ld < 8; st *= 10.0)
      ld++;
   double lots = reqLots;
   if(lots <= 0.0)
      lots = vmin;                 // no lot size entered: the smallest lot this symbol allows
   else
   {
      if(lots < vmin - 1e-9)
         return "Lot size is below the minimum of " + DoubleToString(vmin, ld) + " for " + sym;
      lots = MathFloor(lots / vstep + 1e-9) * vstep;
   }
   lots = NormalizeDouble(lots, ld);
   if(lots > vmax + 1e-9)
      return "Lot size is above the maximum of " + DoubleToString(vmax, ld);
   if(InpMaxLotsPerTrade > 0.0 && lots > InpMaxLotsPerTrade + 1e-9)
      return "Lot size is above the limit you set in the EA (" + DoubleToString(InpMaxLotsPerTrade, ld) + ")";
   ENUM_ORDER_TYPE ot = buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double margin = 0.0;
   if(OrderCalcMargin(ot, sym, lots, price, margin) && margin > AccountInfoDouble(ACCOUNT_MARGIN_FREE))
      return "Not enough free margin for this lot size";

   int    dg = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double sl = NormalizeDouble(buy ? price - slDist : price + slDist, dg);
   double tp = NormalizeDouble(buy ? price + tpDist : price - tpDist, dg);
   if(!StopsAllowed(sym, buy ? (long)POSITION_TYPE_BUY : (long)POSITION_TYPE_SELL, price, sl, tp))
      return "The stop or target is too close for this broker's rules";

   g_trade.SetExpertMagicNumber(26101);
   g_trade.SetTypeFillingBySymbol(sym);
   bool sent = buy ? g_trade.Buy(lots, sym, 0.0, sl, tp, cmt) : g_trade.Sell(lots, sym, 0.0, sl, tp, cmt);
   if(!sent || g_trade.ResultRetcode() != TRADE_RETCODE_DONE)
      return "Broker refused: " + g_trade.ResultRetcodeDescription();
   ok = true;
   return "Opened " + dirS + " " + DoubleToString(lots, ld) + " " + sym + " at " + DoubleToString(g_trade.ResultPrice(), dg)
        + ", stop " + DoubleToString(sl, dg) + ", target " + DoubleToString(tp, dg);
}


//+------------------------------------------------------------------+
//| Pending orders (only when InpAllowPending is on)                 |
//+------------------------------------------------------------------+
bool IsOurs(const string cmt)
{
   return StringFind(cmt, "TLP-") == 0;
}

string OrderTypeText(const long t)
{
   switch((int)t)
   {
      case ORDER_TYPE_BUY_LIMIT:       return "BUY_LIMIT";
      case ORDER_TYPE_SELL_LIMIT:      return "SELL_LIMIT";
      case ORDER_TYPE_BUY_STOP:        return "BUY_STOP";
      case ORDER_TYPE_SELL_STOP:       return "SELL_STOP";
      case ORDER_TYPE_BUY_STOP_LIMIT:  return "BUY_STOP_LIMIT";
      case ORDER_TYPE_SELL_STOP_LIMIT: return "SELL_STOP_LIMIT";
   }
   return "";
}

// The pending orders waiting at the broker, as JSON rows for the app.
string BuildOrders()
{
   string s = "";
   int    added = 0;
   int    total = OrdersTotal();
   for(int i = 0; i < total && added < 50; i++)
   {
      ulong tk = OrderGetTicket(i);   // also selects the order
      if(tk == 0)
         continue;
      string tt = OrderTypeText(OrderGetInteger(ORDER_TYPE));
      if(StringLen(tt) == 0)
         continue;
      string sym = OrderGetString(ORDER_SYMBOL);
      int    dg  = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
      long   exp = OrderGetInteger(ORDER_TIME_EXPIRATION);
      if(exp == 0 && GlobalVariableCheck("TL_X_" + (string)tk))
         exp = (long)GlobalVariableGet("TL_X_" + (string)tk);
      long left = (exp > 0) ? (exp - (long)TimeCurrent()) / 60 : -1;   // minutes left, -1 = no expiry
      if(added > 0)
         s += ",";
      s += "{\"ticket\":" + (string)tk
         + ",\"symbol\":\"" + JsonEscape(sym) + "\""
         + ",\"type\":\"" + tt + "\""
         + ",\"volume\":" + DoubleToString(OrderGetDouble(ORDER_VOLUME_CURRENT), 4)
         + ",\"price\":" + DoubleToString(OrderGetDouble(ORDER_PRICE_OPEN), dg)
         + ",\"limit\":" + DoubleToString(OrderGetDouble(ORDER_PRICE_STOPLIMIT), dg)
         + ",\"sl\":" + DoubleToString(OrderGetDouble(ORDER_SL), dg)
         + ",\"tp\":" + DoubleToString(OrderGetDouble(ORDER_TP), dg)
         + ",\"minutes_left\":" + (string)left
         + ",\"comment\":\"" + JsonEscape(OrderGetString(ORDER_COMMENT)) + "\"}";
      added++;
   }
   return s;
}

// Places ONE pending order that the member asked for with a click. Returns the message to show; ok tells if it worked.
string PlacePending(const string sym, const string pt, const double reqLots, const double priceIn, const double limitIn,
                    const double slIn, const double tpIn, const int expMin, const double ref, const double maxDev,
                    const string cmt, bool &ok)
{
   ok = false;
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
      return "Turn on Algo Trading in MT5";
   if(StringLen(sym) == 0 || priceIn <= 0.0 || slIn <= 0.0 || tpIn <= 0.0)
      return "Incomplete order request";
   ENUM_ORDER_TYPE ot;
   if(pt == "BUY_LIMIT")            ot = ORDER_TYPE_BUY_LIMIT;
   else if(pt == "SELL_LIMIT")      ot = ORDER_TYPE_SELL_LIMIT;
   else if(pt == "BUY_STOP")        ot = ORDER_TYPE_BUY_STOP;
   else if(pt == "SELL_STOP")       ot = ORDER_TYPE_SELL_STOP;
   else if(pt == "BUY_STOP_LIMIT")  ot = ORDER_TYPE_BUY_STOP_LIMIT;
   else if(pt == "SELL_STOP_LIMIT") ot = ORDER_TYPE_SELL_STOP_LIMIT;
   else
      return "Unknown order type";
   bool buy       = (StringFind(pt, "BUY") == 0);
   bool stopLimit = (StringFind(pt, "STOP_LIMIT") >= 0);
   if(!SymbolSelect(sym, true))
      return sym + " is not available in your MT5";
   if(SymbolInfoInteger(sym, SYMBOL_TRADE_MODE) != SYMBOL_TRADE_MODE_FULL)
      return "Trading is not allowed on " + sym + " right now";
   double ask = SymbolInfoDouble(sym, SYMBOL_ASK);
   double bid = SymbolInfoDouble(sym, SYMBOL_BID);
   if(ask <= 0.0 || bid <= 0.0)
      return "No price for " + sym + " right now";
   double mid = (ask + bid) / 2.0;
   if(maxDev > 0.0 && MathAbs(mid - ref) > maxDev)
      return "Price has moved a long way since the analysis (or your broker's prices differ). Analyze again.";

   int    dg    = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   double pt_   = SymbolInfoDouble(sym, SYMBOL_POINT);
   double minD  = MathMax((double)SymbolInfoInteger(sym, SYMBOL_TRADE_STOPS_LEVEL), (double)SymbolInfoInteger(sym, SYMBOL_TRADE_FREEZE_LEVEL)) * pt_ + pt_;
   double price = NormalizeDouble(priceIn, dg);
   double lim   = stopLimit ? NormalizeDouble(limitIn, dg) : 0.0;
   double sl    = NormalizeDouble(slIn, dg);
   double tp    = NormalizeDouble(tpIn, dg);
   double entry = stopLimit ? lim : price;

   // The order price must be on the right side of the market, and far enough from it for this broker.
   if(ot == ORDER_TYPE_BUY_LIMIT && !(price <= ask - minD))
      return "Price is already at or below the buy limit level. Analyze again.";
   if(ot == ORDER_TYPE_SELL_LIMIT && !(price >= bid + minD))
      return "Price is already at or above the sell limit level. Analyze again.";
   if((ot == ORDER_TYPE_BUY_STOP || ot == ORDER_TYPE_BUY_STOP_LIMIT) && !(price >= ask + minD))
      return "Price is already at or above the trigger level. Analyze again.";
   if((ot == ORDER_TYPE_SELL_STOP || ot == ORDER_TYPE_SELL_STOP_LIMIT) && !(price <= bid - minD))
      return "Price is already at or below the trigger level. Analyze again.";
   if(stopLimit)
   {
      if(lim <= 0.0 || (buy && !(lim <= price - minD)) || (!buy && !(lim >= price + minD)))
         return "The limit level is too close to the trigger for this broker's rules";
   }
   if(buy && !(sl <= entry - minD && tp >= entry + minD))
      return "The stop or target is too close to the order price for this broker's rules";
   if(!buy && !(sl >= entry + minD && tp <= entry - minD))
      return "The stop or target is too close to the order price for this broker's rules";

   double vmin  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
   double vstep = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
   double vmax  = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
   int    ld    = 0;
   for(double st = vstep; st < 1.0 - 1e-9 && ld < 8; st *= 10.0)
      ld++;
   double lots = reqLots;
   if(lots <= 0.0)
      lots = vmin;
   else
   {
      if(lots < vmin - 1e-9)
         return "Lot size is below the minimum of " + DoubleToString(vmin, ld) + " for " + sym;
      lots = MathFloor(lots / vstep + 1e-9) * vstep;
   }
   lots = NormalizeDouble(lots, ld);
   if(lots > vmax + 1e-9)
      return "Lot size is above the maximum of " + DoubleToString(vmax, ld);
   if(InpMaxLotsPerTrade > 0.0 && lots > InpMaxLotsPerTrade + 1e-9)
      return "Lot size is above the limit you set in the EA (" + DoubleToString(InpMaxLotsPerTrade, ld) + ")";
   double margin = 0.0;
   ENUM_ORDER_TYPE mt = buy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   if(OrderCalcMargin(mt, sym, lots, entry, margin) && margin > AccountInfoDouble(ACCOUNT_MARGIN_FREE))
      return "Not enough free margin for this lot size";

   // Expiry: let the broker cancel it when possible, otherwise this EA cancels it (see ExpirePending).
   ENUM_ORDER_TYPE_TIME tt = ORDER_TIME_GTC;
   datetime expiry = 0;
   if(expMin > 0)
      expiry = TimeCurrent() + (datetime)(expMin * 60);
   bool brokerExpiry = false;
   if(expiry > 0 && (SymbolInfoInteger(sym, SYMBOL_EXPIRATION_MODE) & SYMBOL_EXPIRATION_SPECIFIED) != 0)
   {
      tt = ORDER_TIME_SPECIFIED;
      brokerExpiry = true;
   }

   g_trade.SetExpertMagicNumber(26101);
   g_trade.SetTypeFillingBySymbol(sym);
   bool sent = g_trade.OrderOpen(sym, ot, lots, lim, price, sl, tp, tt, brokerExpiry ? expiry : 0, cmt);
   uint rc = g_trade.ResultRetcode();
   if(!sent || (rc != TRADE_RETCODE_DONE && rc != TRADE_RETCODE_PLACED))
      return "Broker refused: " + g_trade.ResultRetcodeDescription();
   ulong tk = g_trade.ResultOrder();
   // Do not trust "done" alone: confirm the order really is waiting at the broker, and say why if it is not.
   Sleep(400);
   bool live = (tk > 0 && OrderSelect(tk));
   if(!live)
   {
      string why = "the broker returned no order number";
      bool   filled = false;
      if(tk > 0 && HistoryOrderSelect(tk))
      {
         long stt = HistoryOrderGetInteger(tk, ORDER_STATE);
         filled = (stt == ORDER_STATE_FILLED || stt == ORDER_STATE_PARTIAL);
         why = "the broker's state for it is " + OrderStateText(stt);
      }
      PrintFormat("[TradeLens] %s %s ticket %I64u retcode %u: not waiting after placing (%s)", pt, sym, tk, rc, why);
      if(filled)
      {
         ok = true;
         return pt + " on " + sym + " filled straight away, so it is now an open trade (" + why + ")";
      }
      return "The broker answered OK but the order is not waiting at your broker (" + why + "). Nothing was placed. See the Experts tab in MT5.";
   }
   if(expiry > 0)
      GlobalVariableSet("TL_X_" + (string)tk, (double)expiry);
   PrintFormat("[TradeLens] placed %s %s ticket %I64u at %s", pt, sym, tk, DoubleToString(price, dg));
   ok = true;
   return "Placed " + pt + " " + DoubleToString(lots, ld) + " " + sym + " at " + DoubleToString(price, dg)
        + (stopLimit ? ", limit " + DoubleToString(lim, dg) : "")
        + ", stop " + DoubleToString(sl, dg) + ", target " + DoubleToString(tp, dg)
        + ", ticket " + (string)tk
        + (expiry > 0 ? ", cancels itself in " + (string)(expMin / 60) + "h" + ((expMin % 60) > 0 ? " " + (string)(expMin % 60) + "m" : "") : "");
}

string OrderStateText(const long st)
{
   switch((int)st)
   {
      case ORDER_STATE_STARTED:         return "started";
      case ORDER_STATE_PLACED:          return "placed";
      case ORDER_STATE_CANCELED:        return "cancelled";
      case ORDER_STATE_PARTIAL:         return "partly filled";
      case ORDER_STATE_FILLED:          return "filled";
      case ORDER_STATE_REJECTED:        return "rejected";
      case ORDER_STATE_EXPIRED:         return "expired";
      case ORDER_STATE_REQUEST_ADD:     return "being registered";
      case ORDER_STATE_REQUEST_MODIFY:  return "being modified";
      case ORDER_STATE_REQUEST_CANCEL:  return "being cancelled";
   }
   return "unknown (" + (string)st + ")";
}

// Deletes one waiting pending order. Returns "" when it worked, otherwise the reason.
string CancelPending(const ulong ticket)
{
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
      return "Turn on Algo Trading in MT5";
   if(!OrderSelect(ticket))
      return "That order is not waiting any more";
   if(!g_trade.OrderDelete(ticket))
      return "Broker refused: " + g_trade.ResultRetcodeDescription();
   GlobalVariableDel("TL_X_" + (string)ticket);
   return "";
}

// Housekeeping for orders THIS EA placed (comment starts with TLP-). Runs every timer tick while InpAllowPending is on.
//  - expired: the broker could not hold an expiry, so the EA removes the order when its time is up
//  - missed move: a limit order whose price never came, while price already reached the take profit, is no longer a good plan
void TidyPending()
{
   if(!InpAllowPending || !TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) || !MQLInfoInteger(MQL_TRADE_ALLOWED))
      return;
   for(int i = OrdersTotal() - 1; i >= 0; i--)
   {
      ulong tk = OrderGetTicket(i);
      if(tk == 0 || !IsOurs(OrderGetString(ORDER_COMMENT)))
         continue;
      string sym = OrderGetString(ORDER_SYMBOL);
      long   ty  = OrderGetInteger(ORDER_TYPE);
      double tp  = OrderGetDouble(ORDER_TP);
      string why = "";
      if(OrderGetInteger(ORDER_TIME_EXPIRATION) == 0 && GlobalVariableCheck("TL_X_" + (string)tk)
         && TimeCurrent() >= (datetime)GlobalVariableGet("TL_X_" + (string)tk))
         why = "expired";
      else if(tp > 0.0 && ty == ORDER_TYPE_BUY_LIMIT && SymbolInfoDouble(sym, SYMBOL_BID) >= tp)
         why = "price reached the target without filling it";
      else if(tp > 0.0 && ty == ORDER_TYPE_SELL_LIMIT && SymbolInfoDouble(sym, SYMBOL_ASK) <= tp && SymbolInfoDouble(sym, SYMBOL_ASK) > 0.0)
         why = "price reached the target without filling it";
      if(StringLen(why) == 0)
         continue;
      string name = OrderTypeText(ty) + " " + sym;
      PrintFormat("[TradeLens] cancelling %s ticket %I64u: %s", name, tk, why);
      if(g_trade.OrderDelete(tk))
      {
         GlobalVariableDel("TL_X_" + (string)tk);
         g_cmdMsg = "Cancelled " + name + ": " + why;
         g_lastFull = 0;
      }
   }
}

void PollCommand()
{
   if(!InpAllowRemoteClose && !InpAllowManage && !InpAllowExecute && !InpAllowPending)
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
   if(InpAllowManage)
      ParseManage(resp);
   string cmd  = JsonGet(resp, "cmd");
   if(StringLen(cmd) == 0)
      return;
   long id     = JsonGetNum(resp, "id");
   if(cmd == "open")
   {
      if(!InpAllowExecute)
      {
         ReportResult(id, false, 0, 1, "AllowExecute is switched off in the EA settings");
         return;
      }
      bool   okOpen = false;
      string msgOpen = OpenTrade(JsonGet(resp, "sym"), JsonGet(resp, "dir"), StringToDouble(JsonGet(resp, "lots")),
                                 StringToDouble(JsonGet(resp, "sl_dist")), StringToDouble(JsonGet(resp, "tp_dist")),
                                 StringToDouble(JsonGet(resp, "ref")), StringToDouble(JsonGet(resp, "maxdev")),
                                 JsonGet(resp, "cmt"), okOpen);
      g_cmdMsg = (okOpen ? "Opened from app: " : "Open refused: ") + msgOpen;
      ReportResult(id, okOpen, 0, okOpen ? 0 : 1, msgOpen);
      g_lastFull = 0;
      return;
   }
   if(cmd == "pending_order")
   {
      if(!InpAllowPending)
      {
         ReportResult(id, false, 0, 1, "AllowPending is switched off in the EA settings");
         return;
      }
      bool   okP = false;
      string msgP = PlacePending(JsonGet(resp, "sym"), JsonGet(resp, "ptype"), StringToDouble(JsonGet(resp, "lots")),
                                 StringToDouble(JsonGet(resp, "price")), StringToDouble(JsonGet(resp, "limit")),
                                 StringToDouble(JsonGet(resp, "sl")), StringToDouble(JsonGet(resp, "tp")),
                                 (int)StringToInteger(JsonGet(resp, "exp_min")), StringToDouble(JsonGet(resp, "ref")),
                                 StringToDouble(JsonGet(resp, "maxdev")), JsonGet(resp, "cmt"), okP);
      g_cmdMsg = (okP ? "Pending order: " : "Pending order refused: ") + msgP;
      ReportResult(id, okP, 0, okP ? 0 : 1, msgP);
      g_lastFull = 0;
      return;
   }
   if(cmd == "cancel_order")
   {
      if(!InpAllowPending)
      {
         ReportResult(id, false, 0, 1, "AllowPending is switched off in the EA settings");
         return;
      }
      string errC = CancelPending((ulong)JsonGetNum(resp, "ticket"));
      g_cmdMsg = (StringLen(errC) == 0 ? "Pending order cancelled" : "Cancel refused: " + errC);
      ReportResult(id, StringLen(errC) == 0, 0, StringLen(errC) == 0 ? 0 : 1, StringLen(errC) == 0 ? "Pending order cancelled" : errC);
      g_lastFull = 0;
      return;
   }
   if(!InpAllowRemoteClose)
   {
      ReportResult(id, false, 0, 1, "AllowRemoteClose is switched off in the EA settings");
      return;
   }
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
   int h = g_collapsed ? 34 : 200;

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
      ObjectDelete(0, PFX + "mg");
      ObjectDelete(0, PFX + "ex");
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
   Lbl(PFX + "ex", px + 10, py + 180, (InpAllowExecute ? "Open trade from app: ON" : "Open trade from app: off") + (InpAllowPending ? "  |  Pending: ON" : "  |  Pending: off"), clrSilver, 8);
   Lbl(PFX + "mg", px + 10, py + 162, (StringLen(g_mgMsg) > 0 ? g_mgMsg : (InpAllowManage ? (g_be_on || g_tsl_on || g_ttp_on ? "Auto-manage: ON" : "Auto-manage: allowed, switched off in the app") : "Auto-manage: off")), clrSilver, 8);
   Lbl(PFX + "cmd", px + 10, py + 144, (StringLen(g_cmdMsg) > 0 ? g_cmdMsg : (InpAllowRemoteClose ? "Remote close: ON" : "Remote close: off")), clrSilver, 8);
}

//+------------------------------------------------------------------+
//+------------------------------------------------------------------+
//| Telegram posts (only when InpTgEnable is on)                     |
//| Posts every position opened on this account to your Telegram     |
//| channel, then adds replies under that post: breakeven, trailing  |
//| stop, take-profit moves, partial closes and the final close.     |
//| It only READS positions. It never opens, closes or changes a    |
//| trade because of anything that happens in Telegram.              |
//+------------------------------------------------------------------+
// ==== TELEGRAM BEGIN
struct TgPos
{
   ulong    id;          // position identifier
   long     ptype;       // POSITION_TYPE_BUY or POSITION_TYPE_SELL
   double   popen;       // entry price
   double   vol;         // last seen volume
   double   r;           // original risk distance (1R), 0 when the trade never had a stop
   double   postedSL;    // stop loss as last published
   double   postedTP;    // take profit as last published
   datetime trailSLAt;   // when a trailing stop update was last published
   datetime trailTPAt;   // when a take-profit update was last published
   long     msgId;       // Telegram message id of the original post (0 = unknown)
   bool     be;          // breakeven already published
   bool     silent;      // opened before the EA started: never published
   bool     seen;        // scratch flag used by every scan
   int      goneScans;   // scans since the position left the open list
};

TgPos    g_tgp[];
string   g_tgSym[];
string   g_qText[];
ulong    g_qPid[];
bool     g_qOpen[];
datetime g_qNext[];
int      g_qTries[];
datetime g_tgWarnAt = 0;
datetime g_tgBaselineUntil = 0;

bool TgReady()
{
   if(!InpTgEnable || MQLInfoInteger(MQL_TESTER))
      return false;
   return (StringLen(InpTgToken) > 10 && StringLen(InpTgChatId) > 1);
}

void TgWarn(const string m)
{
   if(TimeLocal() - g_tgWarnAt < 60)
      return;
   g_tgWarnAt = TimeLocal();
   Print("[TradeLens Telegram] ", m);
}

string TgEm(const string k)
{
   if(!InpTgEmoji)
      return "";
   if(k == "buy")
      return ShortToString(0xD83D) + ShortToString(0xDFE2) + " ";
   if(k == "sell")
      return ShortToString(0xD83D) + ShortToString(0xDD34) + " ";
   if(k == "tp")
      return ShortToString(0xD83C) + ShortToString(0xDFAF) + " ";
   if(k == "lock")
      return ShortToString(0xD83D) + ShortToString(0xDD12) + " ";
   if(k == "up")
      return ShortToString(0xD83D) + ShortToString(0xDCC8) + " ";
   if(k == "ok")
      return ShortToString(0x2705) + " ";
   if(k == "bad")
      return ShortToString(0x274C) + " ";
   if(k == "warn")
      return ShortToString(0x26A0) + " ";
   if(k == "flat")
      return ShortToString(0x26AA) + " ";
   if(k == "cut")
      return ShortToString(0x2702) + " ";
   return "";
}

string TgHtml(string s)
{
   StringReplace(s, "&", "&amp;");
   StringReplace(s, "<", "&lt;");
   StringReplace(s, ">", "&gt;");
   return s;
}

string TgPx(const string sym, const double p)
{
   int dg = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);
   return DoubleToString(p, dg);
}

// A price the reader can tap to copy.
string TgCode(const string sym, const double p)
{
   return "<code>" + TgPx(sym, p) + "</code>";
}

string TgUrlEnc(const string s)
{
   uchar b[];
   int n = StringToCharArray(s, b, 0, WHOLE_ARRAY, CP_UTF8);
   string out = "";
   for(int i = 0; i < n - 1; i++)
   {
      uchar c = b[i];
      if((c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || c == '-' || c == '_' || c == '.' || c == '~')
         out += CharToString(c);
      else
         out += StringFormat("%%%02X", c);
   }
   return out;
}

string TgDirName(const long ptype)
{
   return (ptype == POSITION_TYPE_BUY) ? "BUY" : "SELL";
}

int TgFind(const ulong id)
{
   for(int k = 0; k < ArraySize(g_tgp); k++)
      if(g_tgp[k].id == id)
         return k;
   return -1;
}

void TgRemove(const int k)
{
   int n = ArraySize(g_tgp);
   for(int j = k; j < n - 1; j++)
   {
      g_tgp[j]   = g_tgp[j + 1];
      g_tgSym[j] = g_tgSym[j + 1];
   }
   ArrayResize(g_tgp, n - 1);
   ArrayResize(g_tgSym, n - 1);
}

long TgMsgOf(const ulong pid)
{
   int k = TgFind(pid);
   if(k >= 0 && g_tgp[k].msgId > 0)
      return g_tgp[k].msgId;
   string name = "TL_TGM_" + (string)pid;
   if(GlobalVariableCheck(name))
      return (long)GlobalVariableGet(name);
   return 0;
}

void TgEnqueue(const string text, const ulong pid, const bool isOpen)
{
   int n = ArraySize(g_qText);
   if(n >= 60)
      return;                       // a long outage must not pile up messages forever
   ArrayResize(g_qText, n + 1);
   ArrayResize(g_qPid, n + 1);
   ArrayResize(g_qOpen, n + 1);
   ArrayResize(g_qNext, n + 1);
   ArrayResize(g_qTries, n + 1);
   g_qText[n]  = text;
   g_qPid[n]   = pid;
   g_qOpen[n]  = isOpen;
   g_qNext[n]  = 0;
   g_qTries[n] = 0;
}

void TgDequeue(const int k)
{
   int n = ArraySize(g_qText);
   for(int j = k; j < n - 1; j++)
   {
      g_qText[j]  = g_qText[j + 1];
      g_qPid[j]   = g_qPid[j + 1];
      g_qOpen[j]  = g_qOpen[j + 1];
      g_qNext[j]  = g_qNext[j + 1];
      g_qTries[j] = g_qTries[j + 1];
   }
   ArrayResize(g_qText, n - 1);
   ArrayResize(g_qPid, n - 1);
   ArrayResize(g_qOpen, n - 1);
   ArrayResize(g_qNext, n - 1);
   ArrayResize(g_qTries, n - 1);
}

// One call to Telegram's sendMessage. Returns the HTTP code (200 = sent) or -1 when the request could not be made.
int TgHttp(const string text, const long replyTo, long &msgId, int &retry)
{
   msgId = 0;
   retry = 0;
   string body = "chat_id=" + TgUrlEnc(InpTgChatId) + "&text=" + TgUrlEnc(text) + "&parse_mode=HTML&disable_web_page_preview=true";
   if(replyTo > 0)
      body += "&reply_to_message_id=" + (string)replyTo + "&allow_sending_without_reply=true";
   char data[];
   char result[];
   int n = StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   if(n > 0)
      ArrayResize(data, n - 1);
   string rh;
   ResetLastError();
   int code = WebRequest("POST", "https://api.telegram.org/bot" + InpTgToken + "/sendMessage",
                         "Content-Type: application/x-www-form-urlencoded\r\n", 6000, data, result, rh);
   if(code == -1)
   {
      int err = GetLastError();
      if(err == 4014)
         TgWarn("Allow https://api.telegram.org in Tools > Options > Expert Advisors > Allow WebRequest for listed URL");
      else
         TgWarn("network error " + (string)err);
      return -1;
   }
   string resp = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   if(code == 200)
   {
      msgId = JsonGetNum(resp, "message_id");
      return 200;
   }
   retry = (int)JsonGetNum(resp, "retry_after");
   TgWarn("Telegram replied " + (string)code + ": " + JsonGet(resp, "description"));
   return code;
}

// Sends what is waiting, oldest first, at most 3 per call. A failed message is retried later; a wrong token or channel is dropped.
void TgFlush()
{
   if(!TgReady())
      return;
   int sent = 0;
   while(ArraySize(g_qText) > 0 && sent < 3)
   {
      if(g_qNext[0] > TimeLocal())
         return;
      long reply = g_qOpen[0] ? 0 : TgMsgOf(g_qPid[0]);
      long mid   = 0;
      int  retry = 0;
      int  code  = TgHttp(g_qText[0], reply, mid, retry);
      sent++;
      if(code == 200)
      {
         if(g_qOpen[0] && mid > 0)
         {
            GlobalVariableSet("TL_TGM_" + (string)g_qPid[0], (double)mid);
            int k = TgFind(g_qPid[0]);
            if(k >= 0)
               g_tgp[k].msgId = mid;
         }
         TgDequeue(0);
         continue;
      }
      g_qTries[0]++;
      if(code == 400 || code == 401 || code == 403 || code == 404 || g_qTries[0] >= 6)
      {
         TgDequeue(0);
         continue;
      }
      g_qNext[0] = TimeLocal() + (retry > 0 ? retry + 1 : 5 * g_qTries[0]);
      return;
   }
}

// The deal that last reduced or closed this position.
bool TgLastOut(const ulong pid, double &px, long &reason)
{
   if(!HistorySelectByPosition(pid))
      return false;
   bool found = false;
   int n = HistoryDealsTotal();
   for(int i = 0; i < n; i++)
   {
      ulong d = HistoryDealGetTicket(i);
      if(d == 0)
         continue;
      long en = HistoryDealGetInteger(d, DEAL_ENTRY);
      if(en == DEAL_ENTRY_OUT || en == DEAL_ENTRY_OUT_BY || en == DEAL_ENTRY_INOUT)
      {
         px     = HistoryDealGetDouble(d, DEAL_PRICE);
         reason = HistoryDealGetInteger(d, DEAL_REASON);
         found  = true;
      }
   }
   return found;
}

string TgTrimNl(string s)
{
   while(StringLen(s) > 0 && StringGetCharacter(s, StringLen(s) - 1) == '\n')
      s = StringSubstr(s, 0, StringLen(s) - 1);
   return s;
}

string TgFooter()
{
   return "\n\n" + TgHtml(InpTgFooter);
}

void TgPostOpen(const int k, const double sl, const double tp)
{
   string sym = g_tgSym[k];
   double open = g_tgp[k].popen;
   string d = TgDirName(g_tgp[k].ptype);
   string t = TgEm(g_tgp[k].ptype == POSITION_TYPE_BUY ? "buy" : "sell") + "<b>NEW TRADE - " + d + "</b>\n";
   t += "<b>" + TgHtml(sym) + "</b>\n\n";
   t += "Order: Market " + d + "\n";
   t += "Entry: " + TgCode(sym, open) + "\n";
   t += "SL: " + (sl > 0.0 ? TgCode(sym, sl) : "none yet") + "\n";
   t += "TP: " + (tp > 0.0 ? TgCode(sym, tp) : "none yet") + "\n";
   if(sl > 0.0 && tp > 0.0 && MathAbs(open - sl) > 0.0)
      t += "Reward:risk: 1 : " + StringFormat("%.2f", MathAbs(tp - open) / MathAbs(open - sl)) + "\n";
   if(InpTgShowLots)
      t += "Lots: <code>" + DoubleToString(g_tgp[k].vol, 2) + "</code>\n";
   t += "\nTap a number to copy it. If price has already moved far from the entry, skip this one. Updates follow as replies.";
   t += TgFooter();
   TgEnqueue(t, g_tgp[k].id, true);
}

void TgTrackNew(const ulong id, const string sym, const long type, const double open, const double vol, const double sl, const double tp)
{
   int k = ArraySize(g_tgp);
   ArrayResize(g_tgp, k + 1);
   ArrayResize(g_tgSym, k + 1);
   g_tgSym[k]           = sym;
   g_tgp[k].id          = id;
   g_tgp[k].ptype       = type;
   g_tgp[k].popen       = open;
   g_tgp[k].vol         = vol;
   g_tgp[k].r           = InitialRisk(id, open, sl);
   g_tgp[k].postedSL    = sl;
   g_tgp[k].postedTP    = tp;
   g_tgp[k].trailSLAt   = 0;
   g_tgp[k].trailTPAt   = 0;
   g_tgp[k].msgId       = 0;
   g_tgp[k].be          = false;
   g_tgp[k].silent      = false;
   g_tgp[k].seen        = true;
   g_tgp[k].goneScans   = 0;

   double dir = (type == POSITION_TYPE_BUY) ? 1.0 : -1.0;
   double pt  = SymbolInfoDouble(sym, SYMBOL_POINT);
   if(pt <= 0.0)
      pt = 0.00001;
   double tol = MathMax(2.0 * pt, 0.03 * g_tgp[k].r);
   if(sl > 0.0 && dir * (sl - open) >= -tol)
      g_tgp[k].be = true;                 // the stop is already at or past the entry: breakeven would be old news

   string marker = "TL_TGA_" + (string)id;
   if(GlobalVariableCheck(marker))
   {
      // known from an earlier run of the EA: pick up where it left off without announcing again
      g_tgp[k].silent = (GlobalVariableGet(marker) == 2.0);
      g_tgp[k].msgId  = TgMsgOf(id);
      return;
   }
   bool baseline = (TimeLocal() < g_tgBaselineUntil) && !InpTgAnnounceExisting;
   if(baseline)
   {
      g_tgp[k].silent = true;             // already open when the EA started: its first post would be misleading
      GlobalVariableSet(marker, 2.0);
      return;
   }
   GlobalVariableSet(marker, 1.0);
   if(InpTgPostOpen)
      TgPostOpen(k, sl, tp);
   else
      g_tgp[k].silent = false;
}

void TgPostUpdate(const int k, const string title, const string emo, const string body)
{
   string sym = g_tgSym[k];
   string t = TgEm(emo) + "<b>" + title + "</b> - " + TgHtml(sym) + " " + TgDirName(g_tgp[k].ptype) + "\n" + body;
   TgEnqueue(TgTrimNl(t), g_tgp[k].id, false);
}

void TgCheckUpdates(const int k, const double vol, const double sl, const double tp)
{
   string sym = g_tgSym[k];
   double pt  = SymbolInfoDouble(sym, SYMBOL_POINT);
   if(pt <= 0.0)
      pt = 0.00001;
   double dir  = (g_tgp[k].ptype == POSITION_TYPE_BUY) ? 1.0 : -1.0;
   double open = g_tgp[k].popen;
   if(g_tgp[k].r <= 0.0 && sl > 0.0)
      g_tgp[k].r = InitialRisk(g_tgp[k].id, open, sl);   // a stop added later defines 1R
   double R    = g_tgp[k].r;
   double tol  = MathMax(2.0 * pt, 0.03 * R);
   double step = (R > 0.0) ? InpTgTrailStepPct / 100.0 * R : 10.0 * pt;
   datetime now = TimeLocal();
   bool post = !g_tgp[k].silent;

   // partial close: the volume went down but the position is still open
   if(vol < g_tgp[k].vol - 0.0000001)
   {
      double px = 0.0;
      long rs = 0;
      if(post && InpTgPostPartial)
      {
         double pct = (g_tgp[k].vol > 0.0) ? 100.0 * (g_tgp[k].vol - vol) / g_tgp[k].vol : 0.0;
         string b = "Closed about " + StringFormat("%.0f", pct) + "% of the position";
         if(TgLastOut(g_tgp[k].id, px, rs))
            b += " at " + TgCode(sym, px);
         b += ".\nThe rest stays open.";
         TgPostUpdate(k, "PARTIAL CLOSE", "cut", b);
      }
   }
   g_tgp[k].vol = vol;

   string title = "";
   string emo   = "up";
   string body  = "";
   bool beNow   = false;
   bool slTrail = false;
   bool tpMove  = false;

   // ---- stop loss
   if(MathAbs(sl - g_tgp[k].postedSL) > pt * 0.5)
   {
      double was = g_tgp[k].postedSL;
      bool wasBelow   = (was <= 0.0) || (dir * (was - open) < -tol);
      bool nowAtEntry = (sl > 0.0) && (dir * (sl - open) >= -tol);
      if(!g_tgp[k].be && wasBelow && nowAtEntry && (R <= 0.0 || dir * (sl - open) <= 0.5 * R))
      {
         g_tgp[k].be = true;
         if(InpTgPostBE)
         {
            beNow = true;
            body += "Move your SL to: " + TgCode(sym, sl) + "\n";
            body += "(entry was " + TgCode(sym, open) + ")\n";
            g_tgp[k].postedSL = sl;
         }
         else
            g_tgp[k].postedSL = sl;
      }
      else if(sl > 0.0 && was > 0.0 && dir * (sl - was) > 0.0)
      {
         if(dir * (sl - open) >= -tol)
            g_tgp[k].be = true;
         bool bigStep  = (dir * (sl - was) >= step);
         bool waited   = (now - g_tgp[k].trailSLAt >= InpTgTrailSecs);
         if(bigStep && waited)
         {
            g_tgp[k].trailSLAt = now;
            g_tgp[k].postedSL  = sl;
            if(InpTgPostTrailSL)
            {
               slTrail = true;
               body += "New SL: " + TgCode(sym, sl) + " (was " + TgCode(sym, was) + ")\n";
            }
         }
      }
      else if(sl > 0.0 && was > 0.0)
      {
         g_tgp[k].postedSL = sl;
         if(InpTgPostTrailSL)
         {
            slTrail = true;
            emo = "warn";
            body += "SL moved back: " + TgCode(sym, sl) + " (was " + TgCode(sym, was) + ")\n";
         }
      }
      else if(sl > 0.0)
      {
         g_tgp[k].postedSL = sl;
         if(InpTgPostTrailSL)
         {
            slTrail = true;
            body += "SL set: " + TgCode(sym, sl) + "\n";
         }
      }
      else
      {
         g_tgp[k].postedSL = 0.0;
         if(InpTgPostTrailSL)
         {
            slTrail = true;
            emo = "warn";
            body += "SL removed.\n";
         }
      }
   }

   // ---- take profit
   if(MathAbs(tp - g_tgp[k].postedTP) > pt * 0.5)
   {
      double wasTP = g_tgp[k].postedTP;
      bool further = (tp > 0.0 && wasTP > 0.0 && dir * (tp - wasTP) > 0.0);
      bool waitedTP  = (now - g_tgp[k].trailTPAt >= InpTgTrailSecs);
      bool bigStepTP = (dir * (tp - wasTP) >= step);
      if(!further || (bigStepTP && waitedTP))
      {
         g_tgp[k].trailTPAt = now;
         g_tgp[k].postedTP  = tp;
         if(InpTgPostTrailTP)
         {
            tpMove = true;
            if(tp <= 0.0)
               body += "TP removed.\n";
            else if(wasTP <= 0.0)
               body += "TP set: " + TgCode(sym, tp) + "\n";
            else
               body += "New TP: " + TgCode(sym, tp) + " (was " + TgCode(sym, wasTP) + ")\n";
         }
      }
   }

   if(StringLen(body) == 0 || !post)
      return;
   if(beNow)
   {
      title = "BREAKEVEN";
      emo   = "lock";
   }
   else if(slTrail && tpMove)
      title = "TRAILING STOP AND TARGET";
   else if(slTrail)
      title = (emo == "warn") ? "STOP CHANGED" : "TRAILING STOP";
   else
   {
      title = "TAKE PROFIT MOVED";
      emo   = "tp";
   }
   TgPostUpdate(k, title, emo, body);
}

void TgPostClose(const int k, const bool have, const double px, const long reason)
{
   string sym  = g_tgSym[k];
   double dir  = (g_tgp[k].ptype == POSITION_TYPE_BUY) ? 1.0 : -1.0;
   double open = g_tgp[k].popen;
   double R    = g_tgp[k].r;
   double pt   = SymbolInfoDouble(sym, SYMBOL_POINT);
   if(pt <= 0.0)
      pt = 0.00001;
   bool   haveR = (have && R > 0.0);
   double rr    = haveR ? dir * (px - open) / R : 0.0;
   string rs    = ReasonName(reason);
   string label = "Closed";
   string emo   = "flat";
   if(haveR && MathAbs(rr) <= 0.05 && g_tgp[k].be)
   {
      label = "Closed at breakeven";
      emo   = "flat";
   }
   else if(rs == "tp")
   {
      label = "Take profit hit";
      emo   = "ok";
   }
   else if(rs == "sl")
   {
      if(haveR && rr > 0.05)
      {
         label = "Trailing stop hit";
         emo   = "ok";
      }
      else
      {
         label = "Stop loss hit";
         emo   = "bad";
      }
   }
   else if(rs == "so")
   {
      label = "Stopped out by the broker";
      emo   = "bad";
   }
   else if(rs == "manual")
   {
      label = "Closed manually";
      emo   = (haveR && rr > 0.05) ? "ok" : ((haveR && rr < -0.05) ? "bad" : "flat");
   }
   string t = TgEm(emo) + "<b>TRADE CLOSED - " + label + "</b>\n";
   t += "<b>" + TgHtml(sym) + "</b> - " + TgDirName(g_tgp[k].ptype) + "\n\n";
   if(have)
   {
      t += "Entry: " + TgCode(sym, open) + "  Exit: " + TgCode(sym, px) + "\n";
      string r = "Result: ";
      if(haveR)
         r += "<b>" + (rr >= 0.0 ? "+" : "") + StringFormat("%.2f", rr) + "R</b> ";
      double pts = dir * (px - open) / pt;
      r += "(" + (pts >= 0.0 ? "+" : "") + StringFormat("%.0f", pts) + " points)";
      t += r + "\n";
   }
   else
      t += "Entry: " + TgCode(sym, open) + "\nThe exit details were not available yet.\n";
   t = TgTrimNl(t) + TgFooter();
   TgEnqueue(t, g_tgp[k].id, false);
}

void TgScan()
{
   if(!TgReady())
      return;
   int k;
   for(k = 0; k < ArraySize(g_tgp); k++)
      g_tgp[k].seen = false;
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong tk = PositionGetTicket(i);
      if(tk == 0)
         continue;
      if(InpTgMagic >= 0 && PositionGetInteger(POSITION_MAGIC) != InpTgMagic)
         continue;
      ulong  id   = (ulong)PositionGetInteger(POSITION_IDENTIFIER);
      string sym  = PositionGetString(POSITION_SYMBOL);
      long   type = PositionGetInteger(POSITION_TYPE);
      double open = PositionGetDouble(POSITION_PRICE_OPEN);
      double vol  = PositionGetDouble(POSITION_VOLUME);
      double sl   = PositionGetDouble(POSITION_SL);
      double tp   = PositionGetDouble(POSITION_TP);
      k = TgFind(id);
      if(k < 0)
      {
         TgTrackNew(id, sym, type, open, vol, sl, tp);
         continue;
      }
      g_tgp[k].seen = true;
      TgCheckUpdates(k, vol, sl, tp);
   }
   for(k = ArraySize(g_tgp) - 1; k >= 0; k--)
   {
      if(g_tgp[k].seen)
      {
         g_tgp[k].goneScans = 0;
         continue;
      }
      double px = 0.0;
      long   rs = 0;
      bool have = TgLastOut(g_tgp[k].id, px, rs);
      g_tgp[k].goneScans++;
      if(!have && g_tgp[k].goneScans < 5)
         continue;                    // the history can lag a moment behind the position
      if(!g_tgp[k].silent && InpTgPostClose)
         TgPostClose(k, have, px, rs);
      TgRemove(k);
   }
}

void TgInit()
{
   GlobalVariablesDeleteAll("TL_TG", TimeCurrent() - 30 * 86400);   // forget old posts
   if(!TgReady())
      return;
   g_tgBaselineUntil = TimeLocal() + 20;      // trades already open when the EA starts are not announced
   if(InpTgTestOnStart)
      TgEnqueue(TgEm("ok") + "<b>Telegram is connected.</b>\nTrade posts from " + TgHtml(InpTgHeader) + " will appear in this channel.", 0, false);
   TgScan();
   TgFlush();
}
// ==== TELEGRAM END


int OnInit()
{
   g_trade.SetDeviationInPoints(200);
   GlobalVariablesDeleteAll("TL_R_", TimeCurrent() - 30 * 86400);   // forget old positions
   GlobalVariablesDeleteAll("TL_X_", TimeCurrent() - 7 * 86400);    // forget old pending-order expiry times
   g_jSince = TimeCurrent() - (datetime)(MathMax(1, InpJournalDays) * 86400);
   EventSetMillisecondTimer(3000);
   TgInit();
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
   TgScan();
   TgFlush();
   if(!g_paused)
   {
      datetime now = TimeLocal();
      if(g_lastFull == 0 || now - g_lastFull >= MathMax(5, InpIntervalSec))
      {
         g_lastFull = now;
         Sync();
      }
      else if((InpAllowRemoteClose || InpAllowManage || InpAllowExecute || InpAllowPending) && g_net == "Connected")
         PollCommand();
      ManagePositions();
      TidyPending();
   }
   DrawPanel();
   ChartRedraw();
}

void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &request, const MqlTradeResult &result)
{
   if(!InpTgEnable)
      return;
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD || trans.type == TRADE_TRANSACTION_POSITION)
   {
      TgScan();
      TgFlush();
   }
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
