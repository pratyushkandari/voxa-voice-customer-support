'use client';

import React, { useEffect, useRef } from 'react';

interface AudioWaveformProps {
  isActive?: boolean;
  isPlaying?: boolean;
}

export const AudioWaveform: React.FC<AudioWaveformProps> = ({ isActive, isPlaying }) => {
  const active = isActive || isPlaying || false;
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let animationId: number;
    let time = 0;
    const barCount = 42;

    const draw = () => {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const width = canvas.width;
      const height = canvas.height;
      const barWidth = width / barCount - 2;

      time += 0.08;

      for (let i = 0; i < barCount; i++) {
        let barHeight = 4;
        if (active) {
          // Dynamic harmonic waveform calculation
          const wave1 = Math.sin(i * 0.25 + time) * 16;
          const wave2 = Math.cos(i * 0.4 - time * 1.1) * 12;
          const wave3 = Math.sin(i * 0.15 + time * 0.6) * 8;
          barHeight = Math.max(5, Math.abs(wave1 + wave2 + wave3) + 6);
        }

        const x = i * (barWidth + 2);
        const y = (height - barHeight) / 2;

        const gradient = ctx.createLinearGradient(0, y, 0, y + barHeight);
        gradient.addColorStop(0, '#6366f1'); // Indigo top
        gradient.addColorStop(0.5, '#06b6d4'); // Cyan middle
        gradient.addColorStop(1, '#8b5cf6'); // Purple bottom

        ctx.fillStyle = gradient;
        ctx.beginPath();
        if (ctx.roundRect) {
          ctx.roundRect(x, y, barWidth, barHeight, 2);
        } else {
          ctx.rect(x, y, barWidth, barHeight);
        }
        ctx.fill();
      }

      animationId = requestAnimationFrame(draw);
    };

    draw();

    return () => {
      cancelAnimationFrame(animationId);
    };
  }, [active]);

  return (
    <div className="w-full flex items-center justify-center py-1">
      <canvas
        ref={canvasRef}
        width={280}
        height={40}
        className="w-full max-w-[260px] h-[36px] rounded-lg"
      />
    </div>
  );
};
