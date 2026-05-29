import { useEffect, useState } from "react";
import { FileText, Bug, AlertCircle, Activity } from "lucide-react";

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { LogViewer } from "@/components/log-viewer";
import { api } from "@/lib/api";
import { useUIStore } from "@/stores/ui-store";
import type { LogChannel, LogFile } from "@/types/api";
import { fmtBytes, fmtRelativeTime } from "@/lib/format";
import { Badge } from "@/components/ui/badge";

const CHANNELS: { id: LogChannel; label: string; icon: React.ComponentType<{ className?: string }> }[] = [
  { id: "activity", label: "Activity", icon: Activity },
  { id: "error", label: "Errors", icon: AlertCircle },
  { id: "debug", label: "Debug", icon: Bug },
];


export default function LogsPage() {
  const [channel, setChannel] = useState<LogChannel>("activity");
  const [lines, setLines] = useState<string[] | null>(null);
  const [files, setFiles] = useState<LogFile[]>([]);
  const [loading, setLoading] = useState(true);
  const pollInterval = useUIStore((s) => s.pollIntervalMs);
  const paused = useUIStore((s) => s.pollingPaused);

  useEffect(() => {
    api.logs.list().then((r) => setFiles(r.files)).catch(() => setFiles([]));
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function fetchLog() {
      try {
        const r = await api.logs.tail(channel, 500);
        if (!cancelled) {
          setLines(r.lines);
          setLoading(false);
        }
      } catch {
        if (!cancelled) {
          setLines([]);
          setLoading(false);
        }
      }
    }
    setLoading(true);
    fetchLog();
    if (paused) return () => { cancelled = true; };
    const id = setInterval(fetchLog, Math.max(2000, pollInterval));
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [channel, pollInterval, paused]);


  return (
    <div className="space-y-5 animate-fade-up">
      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <FileText className="h-3.5 w-3.5" /> Log files on disk
          </CardTitle>
          <Badge tone="muted">{files.length}</Badge>
        </CardHeader>
        <CardContent className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {files.length === 0 ? (
            <div className="text-xs text-muted-foreground">
              No files in <code className="font-mono">data/logs/</code>.
            </div>
          ) : (
            files.map((f) => (
              <div
                key={f.name}
                className="rounded-md border border-border/60 bg-secondary/30 p-3 text-sm flex items-center gap-3"
              >
                <FileText className="h-4 w-4 text-muted-foreground" />
                <div className="min-w-0 flex-1">
                  <div className="font-medium truncate">{f.name}</div>
                  <div className="text-[11px] text-muted-foreground">
                    {fmtBytes(f.size)} · {fmtRelativeTime(f.mtime)}
                  </div>
                </div>
              </div>
            ))
          )}
        </CardContent>
      </Card>

      <Tabs value={channel} onValueChange={(v) => setChannel(v as LogChannel)}>
        <TabsList>
          {CHANNELS.map((c) => (
            <TabsTrigger key={c.id} value={c.id}>
              <c.icon className="h-3 w-3" />
              {c.label}
            </TabsTrigger>
          ))}
        </TabsList>
        {CHANNELS.map((c) => (
          <TabsContent key={c.id} value={c.id}>
            <LogViewer
              lines={channel === c.id ? lines : null}
              loading={channel === c.id && loading}
              height={520}
            />
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}
