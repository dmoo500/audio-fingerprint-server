export interface Song {
  title: string;
  artists: string[];
  album: string;
  artwork?: string;
}

export interface Candidate {
  metadata: Song;
  match_fraction: number;
}

export interface CaptureResult {
  bytes: ArrayBuffer;
  sampleRate: number;
}

declare global {
  const FingerprintCapture: {
    record(options: {
      workletUrl: string;
      signal: AbortSignal;
      onStart(info: { microphone: string }): void;
    }): Promise<CaptureResult>;
  };
}
