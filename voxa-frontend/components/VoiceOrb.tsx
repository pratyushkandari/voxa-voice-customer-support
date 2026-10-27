'use client';

import React, { useEffect, useRef } from 'react';

interface VoiceOrbProps {
  isSpeaking: boolean;
  isListening: boolean;
  audioLevel?: number; // 0.0 to 1.0
}

export const VoiceOrb: React.FC<VoiceOrbProps> = ({ isSpeaking, isListening, audioLevel = 0 }) => {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  // Props are read through a ref so ONE animation loop runs for the component's lifetime.
  // Restarting the loop on every prop change (audioLevel updates many times a second)
  // reset the wave phase and made the orb stutter.
  const propsRef = useRef({ isSpeaking, isListening, audioLevel });
  propsRef.current = { isSpeaking, isListening, audioLevel };

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    let animationFrameId: number;
    let phase = 0;
    // With reduced motion the orb still reflects state and mic level, but its wave is still.
    const reduceMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;

    const render = () => {
      animationFrameId = requestAnimationFrame(render);
      // The orb lives in a sidebar that is display:none on small screens; skip the
      // drawing work while it is not rendered.
      if (canvas.offsetParent === null) return;

      const { isSpeaking, isListening, audioLevel } = propsRef.current;
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const centerX = canvas.width / 2;
      const centerY = canvas.height / 2;

      // Base radius
      const baseRadius = 55 + (audioLevel * 35);
      if (!reduceMotion) phase += 0.04;

      // Outer glow circle
      const gradient = ctx.createRadialGradient(
        centerX, centerY, baseRadius * 0.2,
        centerX, centerY, baseRadius * 1.6
      );
      
      if (isSpeaking) {
        gradient.addColorStop(0, 'rgba(99, 102, 241, 0.9)');   // Indigo core
        gradient.addColorStop(0.5, 'rgba(6, 182, 212, 0.6)');  // Cyan mid
        gradient.addColorStop(1, 'rgba(99, 102, 241, 0)');     // Fade
      } else if (isListening) {
        gradient.addColorStop(0, 'rgba(236, 72, 153, 0.9)');   // Pink core
        gradient.addColorStop(0.5, 'rgba(168, 85, 247, 0.6)'); // Purple mid
        gradient.addColorStop(1, 'rgba(236, 72, 153, 0)');     // Fade
      } else {
        gradient.addColorStop(0, 'rgba(79, 70, 229, 0.6)');    // Idle glow
        gradient.addColorStop(0.7, 'rgba(15, 23, 42, 0.3)');
        gradient.addColorStop(1, 'rgba(9, 13, 22, 0)');
      }

      ctx.fillStyle = gradient;
      ctx.beginPath();
      ctx.arc(centerX, centerY, baseRadius * 1.6, 0, Math.PI * 2);
      ctx.fill();

      // Pulsing wavy orb body
      ctx.beginPath();
      const points = 32;
      for (let i = 0; i <= points; i++) {
        const angle = (i / points) * Math.PI * 2;
        const wave = Math.sin(angle * 4 + phase) * (isSpeaking || isListening ? 6 + audioLevel * 12 : 3);
        const r = baseRadius + wave;
        const x = centerX + Math.cos(angle) * r;
        const y = centerY + Math.sin(angle) * r;
        if (i === 0) {
          ctx.moveTo(x, y);
        } else {
          ctx.lineTo(x, y);
        }
      }
      ctx.closePath();

      const coreGradient = ctx.createLinearGradient(
        centerX - baseRadius, centerY - baseRadius,
        centerX + baseRadius, centerY + baseRadius
      );
      if (isSpeaking) {
        coreGradient.addColorStop(0, '#6366f1');
        coreGradient.addColorStop(1, '#06b6d4');
      } else if (isListening) {
        coreGradient.addColorStop(0, '#ec4899');
        coreGradient.addColorStop(1, '#8b5cf6');
      } else {
        coreGradient.addColorStop(0, '#312e81');
        coreGradient.addColorStop(1, '#1e1b4b');
      }
      ctx.fillStyle = coreGradient;
      ctx.shadowBlur = 20;
      ctx.shadowColor = isSpeaking ? '#06b6d4' : isListening ? '#ec4899' : '#4f46e5';
      ctx.fill();
      ctx.shadowBlur = 0; // Reset shadow
    };

    render();

    return () => {
      cancelAnimationFrame(animationFrameId);
    };
  }, []);

  return (
    <div className="relative flex items-center justify-center">
      <canvas
        ref={canvasRef}
        width={260}
        height={260}
        className="w-[220px] h-[220px] transition-transform duration-300 transform"
        aria-hidden="true"
      />
    </div>
  );
};
