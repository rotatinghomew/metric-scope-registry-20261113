import argparse
import sys
from .io import read_json, write_json
from .facts import inspect
from .engine import run as run_engine
from .schema import ScopeError


def main():
    parser = argparse.ArgumentParser(
        description='登记请求、应用调用和用户日活三个粒度的事实，并执行版本化指标查询。')
    subparsers = parser.add_subparsers(dest='command')

    inspect_parser = subparsers.add_parser('inspect', help='核对事实表与基础计数')
    inspect_parser.add_argument('--input', required=True)
    inspect_parser.add_argument('--output', required=True)

    query_parser = subparsers.add_parser('query', help='按版本化指标定义执行查询')
    query_parser.add_argument('--input', required=True)
    query_parser.add_argument('--state', required=True)
    query_parser.add_argument('--output', required=True)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 2
    try:
        if args.command == 'inspect':
            document = read_json(args.input)
            write_json(args.output, inspect(document))
        else:
            run_engine(args.input, args.state, args.output)
    except ScopeError as error:
        print('处理失败[%s]：%s' % (error.code, error.message), file=sys.stderr)
        return 2
    except (ValueError, KeyError, TypeError, OSError) as error:
        print('处理失败：' + str(error), file=sys.stderr)
        return 2
    print('结果已保存：' + args.output)
    return 0


if __name__ == '__main__':
    sys.exit(main())
