/**
 * 统一错误类型（单独成文件，供 biWebClient 与 mock/fixtures 共用，避免循环依赖）
 * 错误语义与后端 app.py 的 HTTPException detail 一一对应。
 */

export type BiErrorKind =
  | 'unauthorized' // 401：BI_WEB_TOKEN 开启但没带 Bearer
  | 'bad_request' // 400：参数不在卡的 params_schema 里 / 取值不在 options 里
  | 'not_found' // 404：看板或卡不存在（或看板 disabled / 无卡）
  | 'unavailable' // 503：Nacos 关闸、看板配置解析失败、维度查询失败
  | 'card_error' // 500：卡执行异常
  | 'network'; // 网络不通 / 非预期状态码 / 响应非法

const STATUS_KIND: Record<number, BiErrorKind> = {
  400: 'bad_request',
  401: 'unauthorized',
  404: 'not_found',
  500: 'card_error',
  502: 'unavailable',
  503: 'unavailable',
  504: 'unavailable',
};

export function kindOfStatus(status: number): BiErrorKind {
  return STATUS_KIND[status] ?? (status >= 500 ? 'card_error' : 'network');
}

export class BiWebError extends Error {
  readonly kind: BiErrorKind;
  readonly status: number;

  constructor(kind: BiErrorKind, status: number, message: string) {
    super(message);
    this.name = 'BiWebError';
    this.kind = kind;
    this.status = status;
  }

  /** 终端可读文案（固定字典，不在此拼业务句子）。 */
  toMessage(): string {
    switch (this.kind) {
      case 'unauthorized':
        return '未授权：请检查 BI_WEB_TOKEN 配置';
      case 'bad_request':
        return '请求参数不合法';
      case 'not_found':
        return '看板或卡片不存在';
      case 'unavailable':
        return '服务暂不可用（后端未就绪或已关闸）';
      case 'card_error':
        return '该卡片计算失败';
      default:
        return '网络异常，无法连接后端';
    }
  }
}
