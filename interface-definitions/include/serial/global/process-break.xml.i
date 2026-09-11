<!-- include start from serial/global/process-break.xml.i -->
<node name="process-break">
  <properties>
    <help>Enable process break signals</help>
  </properties>
  <children>
    <leafNode name="ssh-string">
      <properties>
        <help>The break string used for inband SSH break signal processing</help>
        <constraint>
          <regex>.{0,8}</regex>
        </constraint>
        <constraintErrorMessage>Break string too long (limit 8 characters)</constraintErrorMessage>
      </properties>
      <defaultValue>~break</defaultValue>
    </leafNode>
  </children>
</node>
<!-- include end -->
