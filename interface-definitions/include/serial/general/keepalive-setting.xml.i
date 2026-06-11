<!-- include start from serial/general/keepalive-setting.xml.i -->
<node name="keep-alive">
  <properties>
    <help>Keep-alive global setting</help>
  </properties>
  <children>
    <leafNode name="interval">
      <properties>
        <help>Monitor connection interval (in s)</help>
        <valueHelp>
          <format>u32:1-32767</format>
          <description>Decimal integer (1-32767)</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 1-32767"/>
        </constraint>
      </properties>
      <defaultValue>180</defaultValue>
    </leafNode>
    <leafNode name="retries">
      <properties>
        <help>Monitor connection number of retries</help>
        <valueHelp>
          <format>u32:1-32767</format>
          <description>Decimal integer (1-32767)</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 1-32767"/>
        </constraint>
      </properties>
      <defaultValue>5</defaultValue>
    </leafNode>
    <leafNode name="retry-timeout">
      <properties>
        <help>Monitor connection retry timeout (in s)</help>
        <valueHelp>
          <format>u32:1-32767</format>
          <description>Decimal integer (1-32767)</description>
        </valueHelp>
        <constraint>
          <validator name="numeric" argument="--range 1-32767"/>
        </constraint>
      </properties>
      <defaultValue>5</defaultValue>
    </leafNode>
  </children>
</node>
<!-- include end -->
