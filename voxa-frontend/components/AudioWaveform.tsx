'use client';

import React, { useEffect, useRef } from 'react';

interface AudioWaveformProps {
  isActive: boolean;
}

export const AudioWaveform: React.FC<AudioWaveformProps> = ({ isActive }) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let animationId: number;
    let time = 0;
    const barCount = 48;

    const draw = () => {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const width = canvas.width;
      const height = canvas.height;
      const barWidth = width / barCount - 2;

      time += 0.08;

      for (let i = 0; i < barCount; i++) {
        let barHeight = 4;
        if (isActive) {
          // Dynamic harmonic waveform calculation
          const wave1 = Math.sin(i * 0.2 + time) * 20;
          const wave2 = Math.cos(i * 0.35 - time * 1.2) * 15;
          const wave3 = Math.sin(i * 0.1 + time * 0.5) * 10;
          barHeight = Math.max(6, Math.abs(wave1 + wave2 + wave3) + 8);
        }

        const x = i * (barWidth + 2);
        const y = (height - barHeight) / 2;

        const gradient = ctx.createLinearGradient(0, y, 0, y + barHeight);
        gradient.addColorStop(0, '#06b6d4'); // Cyan top
        gradient.addColorStop(0.5, '#6366f1'); // Indigo middle
        gradient.addColorStop(1, '#ec4899'); // Pink bottom

        ctx.fillStyle = gradient;
        ctx.beginPath();
        ctx.roundRect(x, y, barWidth, barHeight, 3);
        ctx.fill();
      }

      animationId = requestAnimationFrame(draw);
    };

    draw();

    return () => {
      cancelAnimationFrame(animationId);
    };
  }, [isActive]);

  return (
    <div className="w-full flex items-center justify-center py-2">
      <canvas
        ref={canvasRef}
        width={360}
        height={60}
        className="w-full max-w-[340px] h-[50px] rounded-lg"
      />
    </div>
  );
};
