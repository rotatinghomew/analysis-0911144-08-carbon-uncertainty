# 共享排放因子与分摊依赖下的碳声明不确定性资料台

本项目保存业务资料的修订版本，提供资料校验、原始记录查询和现有口径的概览核算。操作人员通过本地 HTTP 接口导入资料，分析客户端按版本读取，避免后来的补录覆盖先前资料。

## 环境与运行

使用 Python 3.11，数值计算依赖固定在 requirements.txt。容器使用固定摘要的基础镜像。准备阶段可安装依赖，准备完成后测试与演示只访问本机；无需数据库服务或业务账号。

```sh
docker build -t carbon_uncertainty:local .
docker run --rm --network none carbon_uncertainty:local python -m unittest discover -s tests -v
docker run --rm --network none carbon_uncertainty:local python -m carbon_uncertainty demo
```

也可在 Python 3.11 虚拟环境中安装 requirements.txt 后运行相同的测试和模块命令。demo 在临时目录建库，启动真实本地 HTTP 服务，导入资料，再通过客户端读取结果，退出时关闭服务。运行过程没有固定等待或远程请求。

## 常驻资料接口

```sh
python -m carbon_uncertainty serve --database /tmp/carbon_uncertainty.sqlite3 --load fixtures/demo.json
```

默认监听 127.0.0.1:8080；--host 和 --port 可调整。服务用于可信本地环境，当前不提供公网认证与授权。数据保存在指定 SQLite 文件，重启后仍可按 revision 取回。API 格式见 docs/interfaces.md，业务数据语义见 docs/data-contract.md。

## 目录与现有能力

`carbon_uncertainty/domain.py` 负责资料规则和已有概览；`uncertainty.py` 负责共享来源依赖下的不确定性传播、信息缺口与补测优先级；`comparison.py` 负责两个资料版本之间的修订比较，把资料修订效应与经营活动变化分开；`repository.py` 负责不可变资料版本与导入回执；`service.py` 是服务边界；`client.py` 是独立 HTTP 消费者。fixtures/demo.json 是便于逐项核对的小样本，fixtures/demo_revised.json 是同口径的修订样本（因子更新、活动量变化、分摊变化、缺因子新增），fixtures/history.json 是同口径的本地合成历史资料。测试覆盖领域规则、版本稳定性、不确定性传播边界、修订桥接对账及真实 HTTP 往返。

## 不确定性分析与修订比较

`GET /uncertainty?revision=N` 返回该版本的总量与产品级硬界/标准差、按 source 归并的共享驱动证据、信息缺口与补测排序，以及不被资料支持的风险声明。区间只给硬界、不给覆盖率；缺因子按未知处理而非零排放。`GET /comparison?from=N&to=M` 返回两个不可变版本之间的桥接分解：因子与分摊依据修订属于资料修订效应，活动量、结构与因子挂接变化属于经营活动变化，各项相加与新版本对账。历史版本始终按当时资料解释，字段语义见 docs/interfaces.md，计算口径见 docs/data-contract.md。

现有概览的解释边界跟随输出的 meaning 或 visibility 字段。原始资料和描述性结果不自动构成推断、优化或对外承诺；调用者应保留版本号和摘要。
