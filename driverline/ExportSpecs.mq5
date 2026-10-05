//+------------------------------------------------------------------+
//| ExportSpecs.mq5 - exports Market Watch symbol specs for Driverline|
//| Run on any chart. Output: MQL5/Files/driverline_specs.csv        |
//| Columns: symbol,tick_size,tick_value_loss,vol_min,vol_step,vol_max|
//+------------------------------------------------------------------+
#property script_show_inputs

void OnStart()
{
   string out = "";
   int total = SymbolsTotal(true); // Market Watch symbols only
   for(int i = 0; i < total; i++)
   {
      string s = SymbolName(i, true);
      out += s + "," +
             DoubleToString(SymbolInfoDouble(s, SYMBOL_TRADE_TICK_SIZE), 8) + "," +
             DoubleToString(SymbolInfoDouble(s, SYMBOL_TRADE_TICK_VALUE_LOSS), 8) + "," +
             DoubleToString(SymbolInfoDouble(s, SYMBOL_VOLUME_MIN), 8) + "," +
             DoubleToString(SymbolInfoDouble(s, SYMBOL_VOLUME_STEP), 8) + "," +
             DoubleToString(SymbolInfoDouble(s, SYMBOL_VOLUME_MAX), 8) + "\n";
   }
   int h = FileOpen("driverline_specs.csv", FILE_WRITE | FILE_TXT | FILE_ANSI);
   if(h != INVALID_HANDLE)
   {
      FileWriteString(h, out);
      FileClose(h);
      Print("Driverline specs exported: ", total, " symbols -> MQL5/Files/driverline_specs.csv");
   }
   else
      Print("ExportSpecs: could not open file, error ", GetLastError());
}
