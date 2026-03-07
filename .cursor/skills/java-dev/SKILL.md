---
name: java-dev
description: Provide focused assistance for Java development, including project setup (Maven/Gradle), common project structures, idiomatic code patterns, and debugging. Use when the user mentions Java, JDK, JVM, Maven, Gradle, Spring, or asks for help with Java code, tests, or build configuration.
---

# Java 开发助手

## 使用场景

在下面这些场景下优先启用本技能：

- 用户明确提到「Java」「JDK」「JVM」「Maven」「Gradle」「Spring」「MyBatis」「JUnit」等关键词
- 用户请求：
  - 搭建或修改 Java 工程结构
  - 编写或重构 Java 代码、接口、实体类、工具类
  - 配置 Maven/Gradle 依赖与构建
  - 编写或修复单元测试（JUnit、Mockito 等）
  - 排查 Java 编译错误或运行时异常（NullPointerException、ClassNotFoundException 等）

如果用户需求不是 Java 相关，就不要使用本技能的特定约束，只当普通通用技能处理。

## 总体原则

1. **优先保持与现有项目风格一致**
   - 包名、层次结构、命名规则、日志方案（例如使用 `slf4j` 接口）要尽量沿用项目中已有代码。
2. **遵循 Java 一般最佳实践**
   - 面向接口编程，减少强耦合
   - 避免过多静态工具方法影响测试
   - 适当使用 `Optional`、枚举等提升可读性
3. **保证可编译、可运行**
   - 新增类要放在正确包路径下，类名与文件名一致
   - 引入新依赖时，同步更新 Maven/Gradle 配置文件
4. **重视异常与日志**
   - 避免吞异常：不要简单 `e.printStackTrace()` 然后忽略
   - 使用统一日志框架，带上关键信息（业务主键、上下文）

## 代码组织与结构

在已有项目中：

- **先阅读** `pom.xml` 或 `build.gradle`（如果存在），判断：
  - 使用 Maven 还是 Gradle
  - 使用的 Java 版本（例如 `java.version` 属性或 `sourceCompatibility`）
  - 主要技术栈（Spring Boot、MyBatis、JPA 等）
- **遵循现有分层**：
  - 例如常见的 `controller` / `service` / `repository` / `model` / `config` 等包
  - 新增代码时放到对应层级，避免随意新增顶层包

当需要新增模块或类时：

1. 检查是否已有类似功能类可以复用或扩展。
2. 如果需要新增接口与实现：
   - 将接口放在 `api` 或 `service` 包中
   - 实现类放在 `impl` 或更具体的子包中
3. 对外暴露的 DTO 或 VO 要与已有风格保持一致（字段命名、是否使用 Lombok 等）。

## 代码风格

在没有特殊说明时，采用常见 Java 社区约定：

- 类名使用大驼峰，例如 `UserService`, `OrderController`
- 方法与变量使用小驼峰，例如 `getUserById`, `orderId`
- 常量使用全大写加下划线，例如 `DEFAULT_TIMEOUT_MS`
- 合理拆分方法，避免超长方法（通常不超过 50 行为宜）

如果项目中有专门的代码规范文档（如 `CODING_STYLE.md`），优先按项目规范执行。

## 错误排查与调试

当用户提供 Java 错误信息时：

1. **先定位核心异常**：
   - 关注 `Caused by:` 后面的根因异常
   - 从栈顶向下找到属于项目代码包名的第一行（通常是问题入口）
2. **结合代码分析**：
   - 使用 `Read` 工具阅读涉及的类和方法
   - 检查空指针、类型转换、集合越界、Bean 未注入等常见问题
3. **提出具体修改建议**：
   - 给出需要修改的类名、方法名和大致代码位置
   - 提供修改前后的示例代码片段（使用合适的代码块格式）

对于 Spring/Spring Boot 相关问题：

- 检查注解是否正确（如 `@Service`, `@Repository`, `@Component`, `@Autowired`, `@Value` 等）
- 检查配置文件（`application.yml` / `application.properties`）中的关键配置是否正确
- 如果是 Bean 循环依赖或找不到 Bean，要结合配置和注解分析依赖关系

## Maven / Gradle 依赖管理

当需要添加依赖时：

1. **识别构建工具**：优先查看项目根目录有没有 `pom.xml` 或 `build.gradle`/`build.gradle.kts`。
2. **保持版本一致性**：
   - 优先参考项目中已存在的版本（例如 Spring Boot 父 POM 管理的依赖）
   - 避免在不同模块中使用同一库的多个版本
3. **最小化依赖**：
   - 只添加必要的依赖，避免引入重量级库解决简单问题

在提供依赖示例时：

- Maven 格式示例：

```xml
<dependency>
    <groupId>org.projectlombok</groupId>
    <artifactId>lombok</artifactId>
    <version>REPLACE_WITH_PROJECT_COMPATIBLE_VERSION</version>
    <scope>provided</scope>
</dependency>
```

- Gradle（Groovy）格式示例：

```groovy
dependencies {
    implementation "org.springframework.boot:spring-boot-starter-web"
}
```

实际版本号应尽量根据项目现有配置推断，不随意指定。

## 单元测试与测试用例

当为 Java 代码编写或补充测试时：

1. 先查看项目是否使用 JUnit 4 或 JUnit 5，以及是否使用 Mockito、Spring Test 等。
2. 与现有测试保持一致的：
   - 包结构：通常 `src/test/java` 下与 `src/main/java` 对应包路径
   - 命名规则：如 `*Test` 或 `*Tests`
3. 在给出示例时：
   - 使用清晰的 Arrange-Act-Assert 结构
   - 覆盖正常路径和主要异常路径

示例（JUnit 5 风格）：

```java
@Test
void shouldReturnUserWhenIdExists() {
    // arrange
    when(userRepository.findById(1L)).thenReturn(Optional.of(user));

    // act
    User result = userService.getUserById(1L);

    // assert
    assertNotNull(result);
    assertEquals(1L, result.getId());
}
```

## 与用户交互时的说明风格

当使用本技能回答用户时：

- 回答语言优先使用简体中文，代码与配置使用英文
- 对于新手用户：
  - 适当解释关键概念（例如「什么是 JDK」「Maven 的作用」），但保持简洁
- 对于明显熟悉 Java 的用户：
  - 更偏向直接给出解决方案和关键改动点，少做基础解释

## 不确定性处理

当无法确认具体技术栈或版本时：

1. **先根据现有文件推断**（如 `pom.xml` 中的 groupId/artifactId、依赖列表）。
2. 若仍不明确：
   - 做出最合理的假设（例如主流 Spring Boot 2/3）
   - 在回答中简要说明假设前提，避免过度追问用户。

