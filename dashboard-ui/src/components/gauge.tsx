import { motion } from "framer-motion";
import { cn } from "@/lib/utils";

interface GaugeProps {
  value: number;
  /** Bands [warningAt, dangerAt] expressed as 0-100. */
  bands?: [number, number];
  label?: string;
  sublabel?: string;
  size?: number;
  className?: string;
}

/**
 * Compact dial gauge used by Infrastructure for CPU/RAM/Disk visualisation.
 * Pure SVG, no canvas — keeps it crisp on retina and easy to theme.
 */
export function Gauge({
  value,
  bands = [70, 90],
  label,
  sublabel,
  size = 140,
  className,
}: GaugeProps) {
  const v = Math.max(0, Math.min(100, value));
  const r = (size - 18) / 2;
  const cx = size / 2;
  const cy = size / 2;
  const circumference = Math.PI * r; // half-circle
  const dash = (v / 100) * circumference;

  let toneVar = "var(--success)";
  if (v >= bands[1]) toneVar = "var(--destructive)";
  else if (v >= bands[0]) toneVar = "var(--warning)";

  return (
    <div className={cn("flex flex-col items-center", className)}>
      <svg width={size} height={size / 2 + 14} viewBox={`0 0 ${size} ${size / 2 + 14}`}>
        {/* Track */}
        <path
          d={`M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}`}
          fill="none"
          stroke="hsl(var(--secondary))"
          strokeOpacity={0.6}
          strokeWidth={10}
          strokeLinecap="round"
        />
        {/* Value */}
        <motion.path
          d={`M ${cx - r} ${cy} A ${r} ${r} 0 0 1 ${cx + r} ${cy}`}
          fill="none"
          stroke={`hsl(${toneVar})`}
          strokeWidth={10}
          strokeLinecap="round"
          strokeDasharray={`${dash} ${circumference}`}
          initial={{ strokeDashoffset: circumference }}
          animate={{ strokeDashoffset: 0 }}
          transition={{ type: "spring", damping: 24, stiffness: 80 }}
        />
        {/* Tick marks at bands */}
        {bands.map((b, i) => {
          const a = Math.PI * (1 - b / 100);
          const x1 = cx + Math.cos(a) * (r - 6);
          const y1 = cy - Math.sin(a) * (r - 6);
          const x2 = cx + Math.cos(a) * (r + 6);
          const y2 = cy - Math.sin(a) * (r + 6);
          return (
            <line
              key={i}
              x1={x1}
              y1={y1}
              x2={x2}
              y2={y2}
              stroke="hsl(var(--border))"
              strokeWidth={1.5}
            />
          );
        })}
      </svg>
      <div className="-mt-6 text-center leading-tight">
        <div className="text-2xl font-semibold tracking-tight tabular-nums">
          {v.toFixed(0)}
          <span className="text-sm text-muted-foreground">%</span>
        </div>
        {label && (
          <div className="text-[11px] uppercase tracking-widest text-muted-foreground">
            {label}
          </div>
        )}
        {sublabel && <div className="text-[11px] text-muted-foreground/80">{sublabel}</div>}
      </div>
    </div>
  );
}
