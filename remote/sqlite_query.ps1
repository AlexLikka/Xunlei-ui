$ProgressPreference='SilentlyContinue'
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.Encoding]::UTF8
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class TaskSqlite {
 [DllImport("winsqlite3.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int sqlite3_open_v2(byte[] f,out IntPtr db,int flags,IntPtr vfs);
 [DllImport("winsqlite3.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int sqlite3_prepare_v2(IntPtr db,byte[] sql,int size,out IntPtr stmt,IntPtr tail);
 [DllImport("winsqlite3.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int sqlite3_step(IntPtr stmt);
 [DllImport("winsqlite3.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int sqlite3_column_count(IntPtr stmt);
 [DllImport("winsqlite3.dll", CallingConvention=CallingConvention.Cdecl)] public static extern IntPtr sqlite3_column_name(IntPtr stmt,int n);
 [DllImport("winsqlite3.dll", CallingConvention=CallingConvention.Cdecl)] public static extern IntPtr sqlite3_column_text(IntPtr stmt,int n);
 [DllImport("winsqlite3.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int sqlite3_finalize(IntPtr stmt);
 [DllImport("winsqlite3.dll", CallingConvention=CallingConvention.Cdecl)] public static extern int sqlite3_close(IntPtr db);
 public static string Text(IntPtr ptr) { if(ptr==IntPtr.Zero)return null; int n=0; while(Marshal.ReadByte(ptr,n)!=0)n++; byte[] b=new byte[n]; Marshal.Copy(ptr,b,0,n); return System.Text.Encoding.UTF8.GetString(b); }
}
'@
$db=[IntPtr]::Zero; $stmt=[IntPtr]::Zero
try {
 $path=__DB_PATH__
 $code=[TaskSqlite]::sqlite3_open_v2([Text.Encoding]::UTF8.GetBytes($path+[char]0),[ref]$db,1,[IntPtr]::Zero)
 if($code -ne 0){throw "sqlite open error $code"}
 $sql=__QUERY__
 $code=[TaskSqlite]::sqlite3_prepare_v2($db,[Text.Encoding]::UTF8.GetBytes($sql+[char]0),-1,[ref]$stmt,[IntPtr]::Zero)
 if($code -ne 0){throw "sqlite prepare error $code"}
 $rows=@(); while(($code=[TaskSqlite]::sqlite3_step($stmt)) -eq 100) {
  $row=[ordered]@{}; for($i=0;$i -lt [TaskSqlite]::sqlite3_column_count($stmt);$i++) { $row[[TaskSqlite]::Text([TaskSqlite]::sqlite3_column_name($stmt,$i))]=[TaskSqlite]::Text([TaskSqlite]::sqlite3_column_text($stmt,$i)) }; $rows += [PSCustomObject]$row
 }
 if($code -ne 101){throw "sqlite step error $code"}
 ConvertTo-Json -InputObject @($rows) -Depth 8 -Compress
} finally { if($stmt -ne [IntPtr]::Zero){[void][TaskSqlite]::sqlite3_finalize($stmt)}; if($db -ne [IntPtr]::Zero){[void][TaskSqlite]::sqlite3_close($db)} }
