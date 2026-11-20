import {
  inject,
  onScopeDispose,
  provide,
  shallowRef,
  type InjectionKey,
} from "vue";

type Toast = { message: string; kind: "success" | "error" };
type Notify = (message: string, kind?: Toast["kind"]) => void;
const notifyKey: InjectionKey<Notify> = Symbol("toast");
const toastDurationMs = 4000;

export function provideToast(enabled: () => boolean) {
  const toast = shallowRef<Toast | null>(null);
  let timeout: ReturnType<typeof setTimeout> | undefined;

  function dismiss() {
    clearTimeout(timeout);
    toast.value = null;
  }

  provide(notifyKey, (message, kind = "success") => {
    if (!enabled()) return;
    dismiss();
    toast.value = { message, kind };
    timeout = setTimeout(dismiss, toastDurationMs);
  });
  onScopeDispose(dismiss);
  return { toast, dismiss };
}

export function useToast(): Notify {
  return inject(notifyKey, () => {});
}
